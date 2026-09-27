// Toast stack for things the user needs to notice: action results and errors.
// Non-error toasts auto-expire (App.jsx's pushToast); errors stay until dismissed.
export default function Toasts({ toasts, onDismiss }) {
  if (!toasts.length) return null
  return (
    <div className="fixed right-4 top-16 z-50 flex flex-col gap-2" role="status" aria-live="polite" aria-atomic="false">
      {toasts.map(t => (
        <div key={t.id}
          className={`flex max-w-sm items-start gap-2 rounded-card border bg-panel p-2.5 ${t.level === 'error' ? 'border-bad' : 'border-line'}`}
          style={{ boxShadow: '0 12px 30px -12px rgba(0,0,0,.55)' }}>
          <span className={`text-small ${t.level === 'error' ? 'text-bad' : 'text-ink'}`}>{t.msg}</span>
          <span className="spacer" />
          <button className="quiet !p-0" aria-label="Dismiss notification" onClick={() => onDismiss(t.id)}>✕</button>
        </div>
      ))}
    </div>
  )
}
