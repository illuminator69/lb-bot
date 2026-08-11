import { useApp } from '../App.jsx'
import { ProgressBar, StatusChip } from './ui.jsx'

export function TaskCards({ limit = 6 }) {
  const { state } = useApp()
  const items = Object.values(state.tasks || {})
    .sort((a, b) => (b.started_at || 0) - (a.started_at || 0))
    .slice(0, limit)

  if (!items.length) return <p className="muted">No tasks yet.</p>

  return items.map(t => (
    <div className="rounded-[12px] border border-line bg-panel p-4" key={t.id || t.label}>
      <b className="text-[14px]">{t.label}</b>
      <div className="mt-2.5">
        <ProgressBar value={t.percent || 0} />
      </div>
      <div className="muted mt-1.5 font-mono text-[12px]">{t.status} {t.percent || 0}% {t.current || ''}</div>
      {(t.error || t.summary) && (
        <div className={`mt-1 text-[12.5px] ${t.error ? 'bad' : 'muted'}`}>{t.error || t.summary}</div>
      )}
    </div>
  ))
}
