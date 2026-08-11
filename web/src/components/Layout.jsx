import { memo, useEffect, useRef, useState } from 'react'
import { useApp, SECTIONS, navigate } from '../App.jsx'
import AppearanceMenu from './AppearanceMenu.jsx'

function AdvancedDropdown({ tabs, tab, setTab }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  const active = tabs.includes(tab)

  useEffect(() => {
    function onDocClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [])

  return (
    <div className="relative" ref={ref}>
      <button
        className={`!rounded-[8px] !border-0 !bg-transparent !px-3 !py-1.5 text-[13px] ${active ? '!bg-accent !text-accent-fg font-semibold' : '!text-muted'}`}
        onClick={() => setOpen(o => !o)}
      >
        Advanced ▾
      </button>
      {open && (
        <div className="absolute right-0 top-full z-20 mt-1 flex min-w-[170px] flex-col gap-1 rounded-card border border-line bg-panel p-1 shadow-md">
          {tabs.map(t => (
            <button
              key={t}
              aria-current={t === tab ? 'page' : undefined}
              className={`!border-0 text-left ${t === tab ? 'active' : ''}`}
              onClick={() => { setTab(t); setOpen(false) }}
            >
              {t}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// Its own component so the 1s staleness tick re-renders this span alone — as
// part of the header it re-rendered the whole nav bar every second.
//
// It is a button, not an ornament: the honest job of a live indicator that has
// gone stale is to let you force the resync it is complaining about. Clicking
// runs a real, non-silent refresh of the current screen — no cosmetic timer.
const LiveDot = memo(function LiveDot({ lastSyncAt, busy, onRefresh }) {
  const [, forceTick] = useState(0)
  const [syncing, setSyncing] = useState(false)
  useEffect(() => {
    const t = setInterval(() => forceTick(x => x + 1), 1000)
    return () => clearInterval(t)
  }, [])
  const secs = lastSyncAt ? Math.round((Date.now() - lastSyncAt) / 1000) : null
  const fresh = secs !== null && secs < 12
  const label = syncing
    ? 'syncing…'
    : fresh
      ? `Live · ${busy ? '2s' : '5s'}`
      : (secs === null ? 'connecting…' : `stale ${secs}s`)
  const color = syncing ? 'var(--muted)' : fresh ? 'var(--green)' : 'var(--danger)'

  async function resync() {
    if (syncing) return
    setSyncing(true)
    // force: the point of the button is to pre-empt whatever poll is in flight.
    try { await onRefresh?.({ force: true }) } finally { setSyncing(false) }
  }

  return (
    <button
      title="Refresh now"
      aria-busy={syncing || undefined}
      onClick={resync}
      className="flex items-center gap-1.5 !border-0 !bg-transparent !p-0 text-xs"
      style={{ color }}
    >
      <span className="inline-block h-[7px] w-[7px] rounded-pill" style={{ background: color }} />
      {label}
    </button>
  )
})

export default function Layout({ children }) {
  const { state, refresh } = useApp()
  const { tab, summary, lastSyncAt } = state

  // The tab bar navigates by hash like everything else, so switching tabs is a
  // history entry. Nothing clears the hash any more — it is the source of truth.
  const setTab = navigate

  const needs = summary?.gaps?.needs
  // Contextual header stat per screen, mirroring the mock's headerStat.
  const stat = (() => {
    if (!summary) return ''
    if (tab === 'Fill gaps') return needs ? `${needs} albums need you` : ''
    if (tab === 'Downloads') {
      const t = summary.transfers || {}
      return `${t.active || 0} downloading · ${t.queued || 0} queued`
    }
    if (tab === 'Library') {
      const n = summary.library?.albums
      return n ? `${n.toLocaleString()} albums` : ''
    }
    if (tab === 'System') return 'all systems nominal'
    return ''
  })()

  return (
    <>
      <header className="app-header sticky top-0 z-10 flex flex-wrap items-center gap-[18px] border-b border-line bg-panel px-6 py-3.5">
        {/* The wordmark is the app's "home" affordance — Fill gaps is home. */}
        <h1 className="text-[20px] font-bold tracking-[-0.01em]">
          <button
            title="Go to Fill gaps"
            onClick={() => setTab('Fill gaps')}
            className="!border-0 !bg-transparent !p-0 !text-[20px] !font-bold !text-[color:var(--text)]"
          >
            lb-bot
          </button>
        </h1>
        <nav
          className="flex flex-wrap gap-0.5 rounded-[10px] border border-line p-[3px]"
          style={{ background: 'var(--inset-track)' }}
        >
          {SECTIONS.map(s => {
            if (s.dropdown) {
              return <AdvancedDropdown key={s.key} tabs={s.tabs} tab={tab} setTab={setTab} />
            }
            const active = tab === s.tab
            const badge = s.key === 'Fill gaps' && needs ? ` · ${needs}` : ''
            return (
              <button
                key={s.key}
                aria-current={active ? 'page' : undefined}
                className={`!rounded-[8px] !border-0 !px-3 !py-1.5 text-[13px] ${active ? '!bg-accent !text-accent-fg font-semibold' : '!bg-transparent !text-muted'}`}
                onClick={() => setTab(s.tab)}
              >
                {s.key}{badge}
              </button>
            )
          })}
        </nav>
        <span className="spacer" />
        <span className="context-stat text-[13px] text-muted">{stat}</span>
        <LiveDot lastSyncAt={lastSyncAt} busy={(summary?.transfers?.active || 0) > 0}
          onRefresh={refresh} />
        <AppearanceMenu />
      </header>
      {children}
    </>
  )
}
