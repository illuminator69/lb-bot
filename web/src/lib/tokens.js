// Design tokens for the 2026-10 redesign (design_handoff_lb_bot_frontend/
// redesign-2026-10). The palette is three appearance inputs -- mode
// (system/light/dark) x background (graphite/neutral/warm/plum) x accent
// (cobalt/violet/teal/rose) -- written onto :root as CSS custom properties.
//
// Colour has one job each: the accent is the single main action and the
// current selection, amber means waiting on you, green done, red failed. That
// is why amber, green and red are not offered as accents.
//
// The second half of tokens() derives the names the older components were
// written against (--text, --border, --green, --danger, ...) from the same
// anchors, so a component that has not been restyled yet still follows the
// theme. New code uses the short names: --fg --fg2 --muted --line --surface
// --sunk --accent --wait --ok --bad.

// bg, surface, sunk, line, fg, fg2, muted
const BG = {
  graphite: {
    name: 'Graphite',
    light: ['#f1f2f5', '#ffffff', '#e7e9ee', '#d6d9e1', '#171a21', '#474d5c', '#6c7383'],
    dark: ['#14161b', '#1b1e25', '#0f1115', '#2c303a', '#e9ebf0', '#b6bbc8', '#878e9e'],
  },
  neutral: {
    name: 'Neutral',
    light: ['#f4f4f4', '#ffffff', '#eaeaea', '#d9d9d9', '#161616', '#4a4a4a', '#707070'],
    dark: ['#0e0e0e', '#171717', '#080808', '#2a2a2a', '#ededed', '#b8b8b8', '#8a8a8a'],
  },
  warm: {
    name: 'Warm',
    light: ['#f3efe8', '#fdfbf7', '#e9e3d8', '#d9d1c2', '#221c15', '#51483c', '#776c5e'],
    dark: ['#17130f', '#1e1914', '#0f0c09', '#332b23', '#f2e9dd', '#c4b9aa', '#978b7b'],
  },
  plum: {
    name: 'Plum',
    light: ['#f3eef3', '#fdfafd', '#e9e1e9', '#d9cdd9', '#231b23', '#50454f', '#766a75'],
    dark: ['#181218', '#1f181f', '#0f0b0f', '#332933', '#efe6ee', '#c2b6c1', '#948693'],
  },
}

// accent, text on accent
const AC = {
  cobalt: { name: 'Cobalt', light: ['#2447d6', '#ffffff'], dark: ['#8199ff', '#0c1130'] },
  violet: { name: 'Violet', light: ['#6a3fd0', '#ffffff'], dark: ['#b196ff', '#1a0f3a'] },
  teal: { name: 'Teal', light: ['#0b7a80', '#ffffff'], dark: ['#4fc4c9', '#04282a'] },
  rose: { name: 'Rose', light: ['#c22f63', '#ffffff'], dark: ['#ff8fb0', '#3a0a1c'] },
}

const STATUS = {
  light: { wait: '#955400', waitTint: '#fbeed6', ok: '#1c7448', okTint: '#dcf1e5', bad: '#b3362e', badTint: '#fae2df' },
  dark: { wait: '#e3aa4c', waitTint: '#382c15', ok: '#62c290', okTint: '#16301f', bad: '#f18c82', badTint: '#3b1c19' },
}

export const MODES = [['system', 'System'], ['light', 'Light'], ['dark', 'Dark']]
export const BG_TONES = Object.keys(BG)
export const ACCENT_NAMES = Object.keys(AC)
export const bgName = k => BG[k]?.name || k
export const accentName = k => AC[k]?.name || k
/** The two colours a background swatch is split between, for the mode in use. */
export const bgSwatch = (k, mode) => [BG[k][mode][0], BG[k][mode][1]]
export const accentSwatch = (k, mode) => AC[k][mode][0]

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

export function systemMode() {
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/** 'light' or 'dark' for an appearance whose mode may be 'system'. */
export const resolvedMode = look => (look.mode === 'light' || look.mode === 'dark' ? look.mode : systemMode())

export function tokens(look) {
  const mode = resolvedMode(look)
  const dark = mode === 'dark'
  const [bg, surface, sunk, line, fg, fg2, muted] = (BG[look.bg] || BG.graphite)[mode]
  const [accent, onAccent] = (AC[look.accent] || AC.cobalt)[mode]
  const s = STATUS[mode]
  const accentTint = mix(surface, accent, dark ? 0.20 : 0.14)
  return {
    '--bg': bg, '--surface': surface, '--sunk': sunk, '--line': line,
    '--fg': fg, '--fg2': fg2, '--muted': muted,
    '--accent': accent, '--on-accent': onAccent, '--accent-tint': accentTint,
    '--wait': s.wait, '--wait-tint': s.waitTint,
    '--ok': s.ok, '--ok-tint': s.okTint,
    '--bad': s.bad, '--bad-tint': s.badTint,

    // Names the pre-redesign components use, derived from the same anchors.
    '--text': fg, '--text2': fg2,
    '--faint': mix(muted, bg, 0.18), '--dot': mix(muted, bg, 0.5),
    '--border': line, '--border-warm': line, '--hairline': mix(line, surface, 0.45),
    '--surface-alt': mix(surface, bg, 0.5),
    '--rail': sunk, '--inset-track': sunk, '--inset-deep': sunk, '--inset-warm': mix(surface, sunk, 0.55),
    '--active-item': accentTint, '--sel-row': accentTint,
    '--accent-2': accent, '--accent-3': accent,
    '--accent-btn': accentTint, '--accent-chip': accentTint,
    '--accent-bd-soft': line, '--accent-bd': mix(surface, accent, 0.5), '--accent-bd-sel': accent,
    '--green': s.ok, '--green-on': dark ? '#0c1f15' : '#ffffff',
    '--green-tint': s.okTint, '--green-tint-2': s.okTint, '--green-bd': mix(surface, s.ok, 0.5),
    '--danger': s.bad, '--danger-text': mix(s.bad, fg, 0.35),
    '--danger-tint': s.badTint, '--danger-tint-2': s.badTint, '--danger-bd': mix(surface, s.bad, 0.5),
    '--decide-fg': s.wait, '--decide-tint': s.waitTint, '--warn-tint': s.waitTint,
  }
}

// A new key: the old `lbAppearance` held warm/amber choices that no longer
// exist, and carrying them over would land everyone on a look nobody picked.
const STORAGE_KEY = 'lbLook'
export const DEFAULT_LOOK = { mode: 'system', bg: 'graphite', accent: 'cobalt' }

export function loadAppearance() {
  let v = {}
  try { v = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {} } catch { /* corrupt or blocked -> defaults */ }
  return {
    mode: ['system', 'light', 'dark'].includes(v.mode) ? v.mode : DEFAULT_LOOK.mode,
    bg: BG[v.bg] ? v.bg : DEFAULT_LOOK.bg,
    accent: AC[v.accent] ? v.accent : DEFAULT_LOOK.accent,
  }
}

export function saveAppearance(look) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(look)) } catch { /* per-browser convenience only */ }
}

let themingTimer
export function applyAppearance(look, { animate = false } = {}) {
  const t = tokens(look)
  const el = document.documentElement
  if (animate) {
    // Colours ease over when the user changes them; on first paint they don't.
    el.classList.add('theming')
    clearTimeout(themingTimer)
    themingTimer = setTimeout(() => el.classList.remove('theming'), 380)
  }
  for (const k in t) el.style.setProperty(k, t[k])
  const mode = resolvedMode(look)
  el.dataset.theme = mode
  el.style.colorScheme = mode
}

/** Re-apply when the OS switches light/dark, while the mode is "system". */
export function followSystem(getLook) {
  const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
  if (!mq) return () => {}
  const on = () => { if (getLook().mode === 'system') applyAppearance(getLook(), { animate: true }) }
  mq.addEventListener('change', on)
  return () => mq.removeEventListener('change', on)
}
