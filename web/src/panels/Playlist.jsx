import { useState } from 'react'
import { useApp } from '../App.jsx'
import { TaskCards } from '../components/TaskCards.jsx'
import { PageTitle } from '../components/ui.jsx'

export default function Playlist() {
  const { action } = useApp()
  const [source, setSource] = useState('listenbrainz')
  const [playlist, setPlaylist] = useState('')

  function scan() {
    if (source === 'spotify') action('/api/spotify/scan', { playlist })
    else action('/api/playlists/scan')
  }

  return (
    <div className="mx-auto max-w-[1000px]">
      <div className="mb-[18px]">
        <PageTitle eyebrow="Advanced" title="Playlists" />
      </div>
      <div className="mb-[18px] rounded-[12px] border border-line bg-panel px-4 py-3.5">
        <div className="flex flex-wrap items-center gap-2.5">
          <select value={source} onChange={e => setSource(e.target.value)}>
            <option value="listenbrainz">ListenBrainz</option>
            <option value="spotify">Spotify</option>
          </select>
          {source === 'spotify' && (
            <input
              className="min-w-0 flex-1"
              placeholder="Spotify playlist URL or ID"
              value={playlist}
              onChange={e => setPlaylist(e.target.value)}
            />
          )}
          <button className="primary" onClick={scan}>
            {source === 'spotify' ? 'Scan playlist' : 'Scan ListenBrainz playlists'}
          </button>
        </div>
      </div>
      <div className="settings-grid"><TaskCards /></div>
    </div>
  )
}
