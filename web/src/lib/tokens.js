// Generated design tokens, ported from the lb-bot.dc.html mock's tokens()
// method. The full palette is derived from three appearance inputs --
// theme (dark/light) x bgTone (warm/slate/plum) x accent (amber/sky/sage/rose)
// -- by mixing a small anchor table, and written onto :root as CSS custom
// properties. Components must never hardcode hex except cover gradients and
// the constant dark-on-accent text #17130f.

const BG_ANCHORS = {
  warm: {
    dark: { bg: '#17130f', surf: '#1c1611', text: '#f2e9dd', shade: '#0a0806' },
    light: { bg: '#efe7d8', surf: '#fbf7f0', text: '#2a2118', shade: '#d3c6ae' },
  },
  slate: {
    dark: { bg: '#14161a', surf: '#1a1d22', text: '#e8ecf2', shade: '#080a0c' },
    light: { bg: '#e8ebf0', surf: '#fdfdfe', text: '#1c2128', shade: '#c9cfd8' },
  },
  plum: {
    dark: { bg: '#181218', surf: '#1e171e', text: '#efe6ee', shade: '#0b070b' },
    light: { bg: '#efe6ee', surf: '#fdf9fc', text: '#261e26', shade: '#d2c4d2' },
  },
}

const ACCENTS = { amber: '#e0913f', sky: '#5aa0e0', sage: '#7bab6f', rose: '#d97a8c' }

export const BG_TONES = Object.keys(BG_ANCHORS)
export const ACCENT_NAMES = Object.keys(ACCENTS)
export const ACCENT_SWATCHES = ACCENTS

function hx(h) {
  h = h.replace('#', '')
  if (h.length === 3) h = h.split('').map(c => c + c).join('')
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]
}

function mix(a, b, t) {
  const A = hx(a), B = hx(b)
  return '#' + A.map((v, i) => {
    const n = Math.round(v + (B[i] - v) * t)
    return Math.max(0, Math.min(255, n)).toString(16).padStart(2, '0')
  }).join('')
}

export function tokens({ theme, bgTone, accent: accentName }) {
  const m = mix
  const A = (BG_ANCHORS[bgTone] || BG_ANCHORS.warm)[theme === 'light' ? 'light' : 'dark']
  const { bg, surf, text, shade } = A
  const accent = ACCENTS[accentName] || ACCENTS.amber
  const green = theme === 'dark' ? '#6fae8f' : '#3f8f66'
  const danger = theme === 'dark' ? '#e88a80' : '#c0463a'
  const decide = theme === 'dark' ? '#d9b45f' : '#9a7d2e'
  return {
    '--bg': bg, '--surface': surf, '--surface-alt': m(bg, surf, 0.5),
    '--rail': m(bg, shade, 0.35), '--inset-track': m(bg, shade, 0.55), '--inset-deep': m(bg, shade, 0.80), '--inset-warm': m(bg, shade, 0.42),
    '--active-item': m(surf, accent, 0.05), '--sel-row': m(surf, accent, 0.10),
    '--border': m(surf, text, 0.085), '--border-warm': m(surf, text, 0.12), '--hairline': m(surf, text, 0.045),
    '--text': text, '--text2': m(text, bg, 0.22), '--muted': m(text, bg, 0.42), '--faint': m(text, bg, 0.60), '--dot': m(text, bg, 0.74),
    '--accent': accent,
    '--accent-2': theme === 'dark' ? m(accent, '#ffffff', 0.18) : m(accent, '#000000', 0.28),
    '--accent-3': theme === 'dark' ? m(accent, '#ffffff', 0.35) : m(accent, '#000000', 0.12),
    '--accent-tint': m(accent, surf, 0.90), '--accent-btn': m(accent, surf, 0.86), '--accent-bd-soft': m(accent, surf, 0.80),
    '--accent-bd': m(accent, surf, 0.66), '--accent-bd-sel': m(accent, surf, 0.55), '--accent-chip': m(accent, surf, 0.88),
    '--green': green, '--green-on': theme === 'dark' ? '#0f2019' : '#ffffff', '--green-tint': m(green, surf, 0.86), '--green-tint-2': m(green, surf, 0.91), '--green-bd': m(green, surf, 0.62),
    '--danger': danger, '--danger-text': m(danger, text, 0.30), '--danger-tint': m(danger, surf, 0.86), '--danger-tint-2': m(danger, surf, 0.80), '--danger-bd': m(danger, surf, 0.62),
    '--decide-fg': decide, '--decide-tint': m(decide, surf, 0.88), '--warn-tint': m(accent, surf, 0.90),
  }
}

const STORAGE_KEY = 'lbAppearance'

export function systemTheme() {
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
    ? 'light' : 'dark'
}

export function loadAppearance() {
  let stored = {}
  try { stored = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {} } catch { /* corrupt -> defaults */ }
  return {
    theme: stored.theme === 'light' || stored.theme === 'dark' ? stored.theme : systemTheme(),
    bgTone: BG_TONES.includes(stored.bgTone) ? stored.bgTone : 'warm',
    accent: ACCENT_NAMES.includes(stored.accent) ? stored.accent : 'amber',
  }
}

export function saveAppearance(appearance) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(appearance))
}

export function applyAppearance(appearance) {
  const t = tokens(appearance)
  const el = document.documentElement
  for (const k in t) el.style.setProperty(k, t[k])
  el.style.colorScheme = appearance.theme
}
