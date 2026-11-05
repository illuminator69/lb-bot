import { memo, useEffect, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { SECTIONS, navigateSection, rootSection, isSection } from '../lib/router.js'
import { fillProgress, toFixCount, trackedProgress } from '../lib/needs.js'
import { indexProgress, indexHeaderLabel } from '../lib/indexProgress.js'
import { sectionsFor } from '../lib/access.js'
import { Roll } from './Page.jsx'
import { UPDATED_TEXT, createBuildWatch, readBuild } from '../lib/buildWatch.js'

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
function IndexAtWork({ index, linked = true }) {
  const p = indexProgress(index)
  const label = indexHeaderLabel(p, Date.now() / 1000)
  if (!label) return null
  const [long, short] = label
  const n = x => x.toLocaleString()
  const title = [
    long,
    p.current ? `Scanning ${p.current}` : '',
    p.kind === 'outage' ? `${n(p.done)} of ${n(p.total)} artists are up to date` : '',
    linked ? 'Open System' : '',
  ].filter(Boolean).join(' · ')
  const body = (
    <>
      <span className={`clip ${p.kind === 'outage' ? 'warn' : 'muted'}`}>{short}</span>
      <span className="meter"><i style={{ width: `${Math.max(2, Math.min(100, p.pct))}%` }} /></span>
    </>
  )
  // System is an admin's page: for anyone else the box says what it says and
  // opens nothing.
  return linked
    ? <button className="tray" title={title} aria-label={title} onClick={() => navigateSection('system', 'status')}>{body}</button>
    : <div className="tray" role="status" title={title} aria-label={title}>{body}</div>
}

// Who is signed in (Worbus P1: the Navidrome login), and the way out.
function SignedIn({ session, onSignOut }) {
  return (
    <div className="flex min-w-0 items-center gap-2 px-2">
      <span className="muted clip grow text-caption" title={session.admin ? `${session.user} · admin` : session.user}>
        {session.user}
      </span>
      <button className="sm quiet" onClick={onSignOut}>Sign out</button>
    </div>
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

// The build lb-bot served this page with: lb-bot writes it into index.html
// (`<meta name="lb-bot-build">`). None on the Vite dev server or from an older
// lb-bot, and then the watch takes its first answer instead.
function servedBuild() {
  return readBuild({ build: document.querySelector('meta[name="lb-bot-build"]')?.getAttribute('content') })
}

// B-095: whether lb-bot has been upgraded under this tab (`lib/buildWatch.js`).
// Compared with the build the page was served with, on arrival, then on focus
// and when the tab is shown again, at most once a minute. Never reloads.
function useUpgraded() {
  const [updated, setUpdated] = useState(false)
  useEffect(() => {
    const watch = createBuildWatch({
      baseline: servedBuild(),
      read: async signal => {
        const res = await fetch('/api/health', { cache: 'no-store', signal })
        return res.ok ? readBuild(await res.json()) : null
      },
      onUpdated: () => setUpdated(true),
    })
    const look = () => { if (document.visibilityState !== 'hidden') watch.check() }
    watch.check()
    window.addEventListener('focus', look)
    document.addEventListener('visibilitychange', look)
    return () => {
      window.removeEventListener('focus', look)
      document.removeEventListener('visibilitychange', look)
    }
  }, [])
  return updated
}

export default function Shell({ children }) {
  const { state, refresh, session, isAdmin, downloadsOn, signOut } = useApp()
  const { tab, summary, lastSyncAt, pollError, navKind } = state
  // An artist or album page keeps the section it was opened from lit.
  const lit = isSection(tab) ? tab : (rootSection() || { artist: 'library', album: 'library', place: 'needs' }[tab] || null)
  const fix = toFixCount(state)
  const moving = (summary?.transfers?.active || 0) + (summary?.transfers?.queued || 0)
  const counts = { needs: fix, activity: moving }
  const upgraded = useUpgraded()

  return (
    <div className="app">
      <aside className="side">
        <div className="brand">lb-bot</div>
        <SideSearch />
        <nav className="nav" aria-label="Main">
          {sectionsFor(SECTIONS, { admin: isAdmin, downloads: downloadsOn }).map(([k, label]) => (
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
          {/* Downloads, an admin's, on a server that downloads. */}
          {isAdmin && downloadsOn && <Tray summary={summary} />}
          <IndexAtWork index={summary?.index} linked={isAdmin} />
          <Connection lastSyncAt={lastSyncAt} error={pollError} onRefresh={refresh} />
          <button className="sm quiet text-left" onClick={() => navigateSection('system', 'appearance')}>Appearance</button>
          {session && <SignedIn session={session} onSignOut={signOut} />}
        </div>
      </aside>
      <section className="main" data-anim={navKind || ''}>
        {/* On the page from the start, so that it folds open and shut (N2) —
            rendered only while there was an error, everything under it went
            down a banner's height in one frame, and back in one. While it
            shuts it says what it last said (`Roll` keeps it). The wrapper is
            what the arrival animations leave alone (`.banner-fold` in
            index.css: they select `.main`'s direct children). Each live
            region is on the page, empty, before its text arrives — `Roll`
            mounts what it holds only while open, and a region inserted
            with its text already in it is not announced by every screen
            reader. */}
        <div className="banner-fold">
          <div role="status" aria-live="polite">
            <Roll open={!!pollError}>
              <div className="banner">
                <span className="st bad font-semibold">Can’t reach lb-bot</span>
                <span className="muted">{pollError}</span>
                <span className="spacer" />
                <button className="sm" onClick={() => refresh({ force: true })}>Retry now</button>
              </div>
            </Roll>
          </div>
          {/* Folds open like the one above, and only on a press reloads. */}
          <div role="status" aria-live="polite">
            <Roll open={upgraded}>
              <div className="banner note">
                <span>{UPDATED_TEXT}</span>
                <span className="spacer" />
                <button className="target" onClick={() => window.location.reload()}>Reload</button>
              </div>
            </Roll>
          </div>
        </div>
        {children}
      </section>
    </div>
  )
}
