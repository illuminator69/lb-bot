import { useState } from 'react'
import {
  ACCENT_NAMES, BG_TONES, MODES, accentName, accentSwatch, bgName, bgSwatch,
  loadAppearance, saveAppearance, applyAppearance, resolvedMode,
} from '../lib/tokens.js'
import { Seg } from './Page.jsx'

// System → Appearance. Per browser: nothing here is sent to lb-bot.
export function AppearanceControls() {
  const [look, setLook] = useState(loadAppearance)
  function set(patch) {
    const next = { ...look, ...patch }
    setLook(next)
    applyAppearance(next, { animate: true })
    saveAppearance(next)
  }
  const mode = resolvedMode(look)
  return (
    <>
      <div className="rows" style={{ maxWidth: 640 }}>
        <div className="opt">
          <span>Theme</span>
          <Seg label="Theme" value={look.mode} onChange={m => set({ mode: m })} options={MODES} />
        </div>
        <div className="opt">
          <span>Background</span>
          {BG_TONES.map(k => {
            const [a, b] = bgSwatch(k, mode)
            return (
              <button key={k} className="sw" title={bgName(k)} aria-label={`${bgName(k)} background`}
                aria-pressed={look.bg === k} onClick={() => set({ bg: k })}
                style={{ background: `linear-gradient(135deg, ${a} 50%, ${b} 50%)` }} />
            )
          })}
          <span className="muted">{bgName(look.bg)}</span>
        </div>
        <div className="opt">
          <span>Accent</span>
          {ACCENT_NAMES.map(k => (
            <button key={k} className="sw" title={accentName(k)} aria-label={`${accentName(k)} accent`}
              aria-pressed={look.accent === k} onClick={() => set({ accent: k })}
              style={{ background: accentSwatch(k, mode) }} />
          ))}
          <span className="muted">{accentName(look.accent)}</span>
        </div>
      </div>
      <p className="muted mt-3" style={{ maxWidth: '65ch' }}>
        The accent colours the main action and the current selection. Amber, green and red stay reserved for
        waiting, done and failed, so they are not offered as accents. These choices are kept in this browser.
      </p>
    </>
  )
}
