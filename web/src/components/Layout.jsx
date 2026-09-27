import { memo, useEffect, useState } from 'react'
import { useApp, SECTIONS, navigate } from '../App.jsx'
import AppearanceMenu from './AppearanceMenu.jsx'
import { Menu, MenuItem } from './ui.jsx'

// Its own component so the 1 s staleness tick re-renders this span alone.
// It is a button: the honest job of a live indicator that has gone stale is to
// let you force the resync it is complaining about.
const LiveDot = memo(function LiveDot({ lastSyncAt, busy, onRefresh }) {
  const [, forceTick] = useState(0)
  const [syncing, setSyncing] = useState(false)
  useEffect(() => {
    const t = setInterval(() => forceTick(x => x + 1), 1000)
    return () => clearInterval(t)
  }, [])
  const secs = lastSyncAt ? Math.round((Date.now() - lastSyncAt) / 1000) : null
  const fresh = secs !== null && secs < 12
  const label = syncing ? 'syncing…'
    : fresh ? `Live · ${busy ? '2s' : '5s'}`
    : (secs === null ? 'connecting…' : `stale ${secs}s`)
  const color = syncing ? 'var(--muted)' : fresh ? 'var(--green)' : 'var(--danger)'

  async function resync() {
    if (syncing) return
    setSyncing(true)
    try { await onRefresh?.({ force: true }) } finally { setSyncing(false) }
  }

  return (
    <button title="Refresh now" aria-busy={syncing || undefined} onClick={resync}
      className="flex items-center gap-1.5 whitespace-nowrap !border-0 !bg-transparent !p-0 text-caption"
      style={{ color }}>
      <span className="inline-block h-[7px] w-[7px] rounded-pill" style={{ background: color }} />
      {label}
    </button>
  )
})

// One box for "go get me something": an artist, "Artist – Album", or a pasted
// streaming link. It used to be split between Library's "Add music" bar (which
// sent a pasted link to MusicBrainz as text) and Artist's MusicBrainz search.
function HeaderSearch() {
  const { state } = useApp()
  const current = state.tab === 'Search' ? (state.routeParams[0] || '') : ''
  const [q, setQ] = useState(current)
  useEffect(() => { setQ(current) }, [current])
  return (
    <form role="search" className="app-search min-w-[180px] max-w-[380px] flex-1"
      onSubmit={e => { e.preventDefault(); if (q.trim()) navigate('Search', q.trim()) }}>
      <input type="search" value={q} onChange={e => setQ(e.target.value)}
        aria-label="Search artists and albums, or paste a link"
        placeholder="Search, or paste a link"
        className="w-full !py-1.5 text-small" />
    </form>
  )
}

export default function Layout({ children }) {
  const { state, refresh } = useApp()
  const { tab, summary, lastSyncAt, health } = state

  const needs = summary?.gaps?.needs
  // What this screen is about, in one line, from data the screen already has.
  // The System tab used to print a hardcoded "all systems nominal" here.
  const stat = (() => {
    if (!summary) return ''
    if (tab === 'Fill gaps') return needs ? `${needs.toLocaleString()} albums need you` : 'nothing needs you'
    if (tab === 'Downloads') {
      const t = summary.transfers || {}
      return `${t.active || 0} downloading · ${t.queued || 0} queued`
    }
    if (tab === 'Library') {
      const l = summary.library || {}
      return l.albums ? `${l.albums.toLocaleString()} albums · ${(l.withGaps || 0).toLocaleString()} with gaps` : ''
    }
    if (tab === 'Settings' && health?.checks) {
      const failing = health.checks.filter(c => !c.ok).length
      return failing ? `${failing} check${failing === 1 ? '' : 's'} failing` : 'all checks passing'
    }
    return ''
  })()
  const statColor = tab === 'Settings' && health?.checks?.some(c => !c.ok) ? 'var(--danger)' : undefined

  const navButton = (s) => {
    const active = tab === s
    const badge = s === 'Fill gaps' && needs ? ` · ${needs.toLocaleString()}` : ''
    return (
      <button key={s}
        aria-current={active ? 'page' : undefined}
        className={`!rounded-ctl !border-0 !px-3 !py-1.5 text-small ${active ? '!bg-accent !text-accent-fg font-semibold' : '!bg-transparent !text-muted'}`}
        onClick={() => navigate(s)}>
        {s}{badge}
      </button>
    )
  }

  return (
    <>
      <header className="app-header sticky top-0 z-20 flex flex-wrap items-center gap-x-[18px] gap-y-2.5 border-b border-line bg-panel px-6 py-3">
        <button title="Go to Fill gaps" onClick={() => navigate('Fill gaps')}
          className="!border-0 !bg-transparent !p-0 text-title font-bold !text-[color:var(--text)]">
          lb-bot
        </button>
        <nav aria-label="Main" className="app-nav flex flex-wrap gap-0.5 rounded-card border border-line p-[3px]"
          style={{ background: 'var(--inset-track)' }}>
          {SECTIONS.map(navButton)}
        </nav>
        <Menu label={`☰ ${SECTIONS.includes(tab) ? tab : 'Menu'}`} title="Sections"
          buttonClass="nav-menu-btn items-center gap-1.5 !py-1.5 text-small">
          {close => SECTIONS.map(s => (
            <MenuItem key={s} active={tab === s} onClick={() => { close(); navigate(s) }}>
              {s}{s === 'Fill gaps' && needs ? ` · ${needs.toLocaleString()}` : ''}
            </MenuItem>
          ))}
        </Menu>
        <HeaderSearch />
        <span className="spacer" />
        <span className="context-stat whitespace-nowrap text-small text-muted" style={statColor ? { color: statColor } : undefined}>{stat}</span>
        <LiveDot lastSyncAt={lastSyncAt} busy={(summary?.transfers?.active || 0) > 0} onRefresh={refresh} />
        <AppearanceMenu />
      </header>
      {children}
    </>
  )
}
