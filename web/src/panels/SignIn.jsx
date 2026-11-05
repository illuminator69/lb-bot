import { useState } from 'react'
import { api } from '../lib/api.js'

// Worbus P1 (S6): lb-bot's web UI is behind the Navidrome login. Any Navidrome
// user signs in with their own username and password; lb-bot checks them
// against Navidrome and keeps neither. Shown for a 401 from any route.
const SAID = {
  bad_credentials: 'That username and password did not match a Navidrome account.',
  too_many_attempts: 'Too many attempts. Wait a few minutes, then try again.',
  navidrome_unavailable: 'Navidrome did not answer. Try again in a moment.',
}

export default function SignIn({ onSignedIn }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(e) {
    e.preventDefault()
    if (busy || !username.trim() || !password) return
    setBusy(true)
    setError('')
    try {
      const who = await api('/api/session', {
        method: 'POST', body: JSON.stringify({ username: username.trim(), password }),
      })
      setPassword('')
      onSignedIn(who)
    } catch (err) {
      setError(SAID[err.payload?.code] || `Could not sign in: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="flex h-full items-center justify-center p-4" style={{ background: 'var(--bg)' }}>
      <form className="box flex w-full flex-col gap-3" style={{ maxWidth: 360 }} onSubmit={submit}
        aria-labelledby="signin-title">
        <div>
          <h1 id="signin-title" className="brand !p-0">lb-bot</h1>
          <p className="muted mt-1">Sign in with your Navidrome account.</p>
        </div>
        <label className="flex flex-col gap-1">
          <span className="s">Username</span>
          <input autoComplete="username" autoFocus value={username}
            onChange={e => setUsername(e.target.value)} disabled={busy} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="s">Password</span>
          <input type="password" autoComplete="current-password" value={password}
            onChange={e => setPassword(e.target.value)} disabled={busy} />
        </label>
        {error && <p className="why bad !m-0" role="alert">{error}</p>}
        <button type="submit" className="primary" disabled={busy || !username.trim() || !password}
          aria-busy={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>
    </main>
  )
}
