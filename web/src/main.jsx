import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'
import './index.css'
import { loadAppearance, applyAppearance, followSystem } from './lib/tokens.js'
import App from './App.jsx'

// Theme before first paint so there is no flash of the fallback palette.
applyAppearance(loadAppearance())
// "System" follows the OS when it switches between light and dark.
followSystem(loadAppearance)

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
