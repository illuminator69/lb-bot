import { useState } from 'react'
import {
  ACCENT_NAMES, ACCENT_SWATCHES, BG_TONES,
  loadAppearance, saveAppearance, applyAppearance,
} from '../lib/tokens.js'
import { Menu } from './ui.jsx'

const TONE_LABELS = { warm: 'Warm', slate: 'Slate', plum: 'Plum' }
// Representative surface colour per tone for the swatch.
const TONE_DOTS = { warm: '#1c1611', slate: '#1a1d22', plum: '#1e171e' }

function useAppearance() {
  const [appearance, setAppearance] = useState(loadAppearance)
  function set(patch) {
    setAppearance(prev => {
      const next = { ...prev, ...patch }
      applyAppearance(next)
      try { saveAppearance(next) } catch { /* per-viewer convenience only */ }
      return next
    })
  }
  return [appearance, set]
}

const Label = ({ children }) => (
  <div className="mb-1.5 text-micro font-semibold uppercase tracking-[.1em] text-muted">{children}</div>
)

// The controls themselves — in the header popover and on Settings → Appearance.
export function AppearanceControls() {
  const [appearance, set] = useAppearance()
  return (
    <div>
      <Label>Theme</Label>
      <div className="mb-3 flex gap-1 rounded-card border border-line p-[3px]" style={{ background: 'var(--inset-track)' }}>
        {['dark', 'light'].map(t => (
          <button key={t} aria-pressed={appearance.theme === t}
            className={`flex-1 !rounded-ctl !border-0 !py-1.5 text-caption ${appearance.theme === t ? '!bg-accent !text-accent-fg font-semibold' : '!bg-transparent !text-muted'}`}
            onClick={() => set({ theme: t })}>
            {t === 'dark' ? '☾ Dark' : '☀ Light'}
          </button>
        ))}
      </div>
      <Label>Accent</Label>
      <div className="mb-3 flex gap-2.5">
        {ACCENT_NAMES.map(a => (
          <button key={a} aria-label={`${a} accent`} title={a} aria-pressed={appearance.accent === a}
            className="h-7 w-7 !rounded-pill !p-0"
            style={{
              background: ACCENT_SWATCHES[a],
              borderColor: appearance.accent === a ? 'var(--text)' : 'transparent',
              boxShadow: appearance.accent === a ? '0 0 0 2px var(--surface) inset' : 'none',
            }}
            onClick={() => set({ accent: a })} />
        ))}
      </div>
      <Label>Background</Label>
      <div className="flex gap-1.5">
        {BG_TONES.map(t => {
          const active = appearance.bgTone === t
          return (
            <button key={t} aria-pressed={active}
              className="flex flex-1 items-center justify-center gap-[7px] !py-[7px] text-caption"
              style={{
                borderColor: active ? 'var(--accent)' : 'var(--border)',
                background: active ? 'var(--accent-tint)' : 'var(--inset-warm)',
                color: active ? 'var(--accent)' : 'var(--text2)',
                fontWeight: active ? 600 : 400,
              }}
              onClick={() => set({ bgTone: t })}>
              <span className="h-3.5 w-3.5 shrink-0 rounded-[4px] border border-line" style={{ background: TONE_DOTS[t] }} />
              {TONE_LABELS[t]}
            </button>
          )
        })}
      </div>
    </div>
  )
}

export default function AppearanceMenu() {
  return (
    <Menu align="right" width={268} title="Appearance" ariaLabel="Appearance" label="◑"
      buttonClass="h-[34px] w-[34px] !p-0 text-lead leading-none"
      buttonStyle={{ background: 'var(--inset-warm)', color: 'var(--text2)' }}>
      <div className="p-2"><AppearanceControls /></div>
    </Menu>
  )
}
