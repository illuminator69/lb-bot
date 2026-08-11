import { useEffect, useRef, useState } from 'react'
import {
  ACCENT_NAMES, ACCENT_SWATCHES, BG_TONES,
  loadAppearance, saveAppearance, applyAppearance,
} from '../lib/tokens.js'

const TONE_LABELS = { warm: 'Warm', slate: 'Slate', plum: 'Plum' }
// Representative surface color per tone for the swatch dot (mock bgDefs).
const TONE_DOTS = { warm: '#1c1611', slate: '#1a1d22', plum: '#1e171e' }

export default function AppearanceMenu() {
  const [open, setOpen] = useState(false)
  const [appearance, setAppearance] = useState(loadAppearance)
  const ref = useRef(null)

  useEffect(() => {
    function onDocClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [])

  function set(patch) {
    setAppearance(prev => {
      const next = { ...prev, ...patch }
      applyAppearance(next)
      saveAppearance(next)
      return next
    })
  }

  return (
    <div className="relative" ref={ref}>
      <button
        aria-label="Appearance"
        title="Appearance"
        className="h-[34px] w-[34px] !rounded-[9px] !p-0 text-[16px] leading-none"
        style={{ background: 'var(--inset-warm)', color: 'var(--text2)' }}
        onClick={() => setOpen(o => !o)}
      >
        ◑
      </button>
      {open && (
        <div
          className="absolute right-0 top-full z-30 mt-1.5 w-[268px] rounded-[14px] border border-line bg-panel p-3.5"
          style={{ boxShadow: '0 20px 48px -14px rgba(0,0,0,.6)' }}
        >
          <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-[.1em] text-muted">Theme</div>
          <div className="mb-3 flex gap-1 rounded-[10px] border border-line p-[3px]" style={{ background: 'var(--inset-track)' }}>
            {['dark', 'light'].map(t => (
              <button
                key={t}
                className={`flex-1 !rounded-[8px] !border-0 !py-1.5 text-[12.5px] ${appearance.theme === t ? '!bg-accent !text-accent-fg font-semibold' : '!bg-transparent !text-muted'}`}
                onClick={() => set({ theme: t })}
              >
                {t === 'dark' ? '☾ Dark' : '☀ Light'}
              </button>
            ))}
          </div>
          <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-[.1em] text-muted">Accent</div>
          <div className="mb-3 flex gap-2.5">
            {ACCENT_NAMES.map(a => (
              <button
                key={a}
                aria-label={a}
                title={a}
                className="h-7 w-7 !rounded-pill !p-0"
                style={{
                  background: ACCENT_SWATCHES[a],
                  borderColor: appearance.accent === a ? 'var(--text)' : 'transparent',
                  boxShadow: appearance.accent === a ? '0 0 0 2px var(--surface) inset' : 'none',
                }}
                onClick={() => set({ accent: a })}
              />
            ))}
          </div>
          <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-[.1em] text-muted">Background</div>
          <div className="flex gap-1.5">
            {BG_TONES.map(t => {
              const active = appearance.bgTone === t
              return (
                <button
                  key={t}
                  className="flex flex-1 items-center justify-center gap-[7px] !rounded-[8px] !py-[7px] text-[12.5px]"
                  style={{
                    borderColor: active ? 'var(--accent)' : 'var(--border)',
                    background: active ? 'var(--accent-tint)' : 'var(--inset-warm)',
                    color: active ? 'var(--accent)' : 'var(--text2)',
                    fontWeight: active ? 600 : 400,
                  }}
                  onClick={() => set({ bgTone: t })}
                >
                  <span className="h-3.5 w-3.5 shrink-0 rounded-[4px] border border-line"
                    style={{ background: TONE_DOTS[t] }} />
                  {TONE_LABELS[t]}
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
