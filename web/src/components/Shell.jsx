import { memo, useEffect, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { SECTIONS, navigateSection, rootSection, isSection } from '../lib/router.js'
import { fillProgress, toFixCount, trackedProgress } from '../lib/needs.js'
import { indexProgress, indexHeaderLabel } from '../lib/indexProgress.js'
import { Roll } from './Page.jsx'

// Connection status, in its one fixed place. It is a button: the honest job of
// a status that has gone stale is to let you force the resync it is
// complaining about. Its own component so the 1 s tick re-renders it alone.
const Connection = memo(function Connection({ lastSyncAt, error, onRefresh }) {
  const [, forceTick] = useState(0)
  const [syncing, setSyncing] = useState(false)
  useEffect(() => {
    const t = setInterval(() => forceTick(x => x + 1), 1000)
    return () => clearInterval(t)
  }, [])
  const secs = lastSyncAt ? Math.round((Date.now() - lastSyncAt) / 1000) : null
  const fresh = secs !== null && secs < 12 && !error
  const tone = syncing ? '' : fresh ? 'ok' : secs === null && !error ? '' : 'bad'
  const label = syncing ? 'Syncing…'
    : fresh ? `Connected · synced ${secs} s ago`
    : secs === null ? (error ? 'Can’t reach lb-bot' : 'Connecting…')
    : `No answer for ${secs} s`

  async function resync() {
    if (syncing) return
    setSyncing(true)
    try { await onRefresh?.({ force: true }) } finally { setSyncing(false) }
  }

  return (
    <button className="quiet sm !px-2 text-left" title="Sync now" onClick={resync}>
      <span className={`st ${tone}`}>{label}</span>
    </button>
  )
})

// Running downloads, always in view. Album-level when the server says which
// album a transfer belongs to, file-level otherwise.
function Tray({ summary }) {
  const t = summary?.transfers || {}
  const rows = summary?.activeTransfers || []
  const byAlbum = new Map()
  for (const r of rows) {
    const key = r.groupId || r.album || r.id
    const g = byAlbum.get(key) || { key, groupId: r.groupId, title: r.album || r.title, rows: [], active: false }
    g.rows.push(r)
    g.active = g.active || r.state === 'active'
    byAlbum.set(key, g)
  }
  const groups = [...byAlbum.values()].sort((a, b) => b.active - a.active).slice(0, 3)
  const active = t.active || 0
  const queued = t.queued || 0
  const head = active ? `${active} downloading${queued ? ` · ${queued} queued` : ''}`
    : queued ? `${queued} queued` : 'Nothing downloading'
  return (
    <button className="tray" title="Open Activity" onClick={() => navigateSection('activity', 'now')}>
      <span><span className={`st ${active ? 'run' : queued ? 'wait' : ''}`}>{head}</span></span>
      {groups.map((g, i) => {
        // The album's own progress where the server gives it; otherwise worked
        // out from the files still listed against the most there have been.
        const pct = (g.groupId && fillProgress(summary, g.groupId)?.pct) ?? trackedProgress(`tray:${g.key}`, g.rows).pct
        return (
          <span key={g.groupId || i} className="flex min-w-0 flex-col gap-1">
            <span className="muted clip">{g.title} · {pct}%</span>
            <span className="meter"><i style={{ width: `${pct}%` }} /></span>
          </span>
        )
      })}
    </button>
  )
}

// The library index at work, on every page (ruling U1): after a version bump
// the background worker walks every artist for hours, and the only sign of it
// was a row under System. One box of the tray's own kind, under the tray:
// what `indexHeaderLabel` says, over a meter. Nothing is rendered when
// nothing is measured, nothing is left to do, or all that is left waits for a
// retry (`indexProgress`) — a sign that could never leave says nothing.
//
// The sidebar is narrower than the label's long form, so the short one is
// shown and the long one is the button's title; a label that still does not
// fit is clipped, never wrapped. It opens System › Status, where the same
// numbers have their rows.
function IndexAtWork({ index }) {
  const p = indexProgress(index)
  const label = indexHeaderLabel(p, Date.now() / 1000)
  if (!label) return null
  const [long, short] = label
  const n = x => x.toLocaleString()
  const title = [
    long,
    p.current ? `Scanning ${p.current}` : '',
    p.kind === 'outage' ? `${n(p.done)} of ${n(p.total)} artists are up to date` : '',
    'Open System',
  ].filter(Boolean).join(' · ')
  return (
    <button className="tray" title={title} aria-label={title} onClick={() => navigateSection('system', 'status')}>
      <span className={`clip ${p.kind === 'outage' ? 'warn' : 'muted'}`}>{short}</span>
      <span className="meter"><i style={{ width: `${Math.max(2, Math.min(100, p.pct))}%` }} /></span>
    </button>
  )
}

function SideSearch() {
  const { state } = useApp()
  const current = state.tab === 'search' ? (state.routeParams[0] || '') : ''
  const [q, setQ] = useState(current)
  useEffect(() => { setQ(current) }, [current])
  return (
    <form role="search" onSubmit={e => { e.preventDefault(); if (q.trim()) navigate('search', q.trim()) }}>
      <input type="search" value={q} onChange={e => setQ(e.target.value)}
        aria-label="Search artists and albums, or paste a link"
        placeholder="Search or paste a link" />
    </form>
  )
}

export default function Shell({ children }) {
  const { state, refresh } = useApp()
  const { tab, summary, lastSyncAt, pollError, navKind } = state
  // An artist or album page keeps the section it was opened from lit.
  const lit = isSection(tab) ? tab : (rootSection() || { artist: 'library', album: 'library', place: 'needs' }[tab] || null)
  const fix = toFixCount(state)
  const moving = (summary?.transfers?.active || 0) + (summary?.transfers?.queued || 0)
  const counts = { needs: fix, activity: moving }

  return (
    <div className="app">
      <aside className="side">
        <div className="brand">lb-bot</div>
        <SideSearch />
        <nav className="nav" aria-label="Main">
          {SECTIONS.map(([k, label]) => (
            <button key={k} aria-current={lit === k ? 'page' : undefined} onClick={() => navigateSection(k)}>
              {label}
              {counts[k] ? (
                <span className={`count ${k === 'needs' ? 'wait' : ''}`}
                  title={k === 'needs' ? `${counts[k]} to fix` : `${counts[k]} downloading or queued`}>
                  {counts[k].toLocaleString()}
                </span>
              ) : null}
            </button>
          ))}
        </nav>
        <div className="side-foot">
          <Tray summary={summary} />
          <IndexAtWork index={summary?.index} />
          <Connection lastSyncAt={lastSyncAt} error={pollError} onRefresh={refresh} />
          <button className="sm quiet text-left" onClick={() => navigateSection('system', 'appearance')}>Appearance</button>
        </div>
      </aside>
      <section className="main" data-anim={navKind || ''}>
        {/* On the page from the start, so that it folds open and shut (N2) —
            rendered only while there was an error, everything under it went
            down a banner's height in one frame, and back in one. While it
            shuts it says what it last said (`Roll` keeps it). The wrapper is
            what the arrival animations leave alone (`.banner-fold` in
            index.css: they select `.main`'s direct children). */}
        <div className="banner-fold">
          <Roll open={!!pollError}>
            <div className="banner" role="status" aria-live="polite">
              <span className="st bad font-semibold">Can’t reach lb-bot</span>
              <span className="muted">{pollError}</span>
              <span className="spacer" />
              <button className="sm" onClick={() => refresh({ force: true })}>Retry now</button>
            </div>
          </Roll>
        </div>
        {children}
      </section>
    </div>
  )
}
