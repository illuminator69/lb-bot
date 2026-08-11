// Small toast stack, replacing the single overwritable syncMsg string for
// anything the user actually needs to notice: action results and errors.
// Non-error toasts auto-expire (see App.jsx's pushToast); error toasts stay
// until dismissed so they can't be silently clobbered by the next poll.
export default function Toasts({ toasts, onDismiss }) {
  if (!toasts.length) return null

  return (
    <div className="fixed right-4 top-16 z-50 flex flex-col gap-2"
      role="status" aria-live="polite" aria-atomic="false">
      {toasts.map(t => (
        <div
          key={t.id}
          className={`flex max-w-sm items-start gap-2 rounded-card border p-2.5 shadow-md ${
            t.level === 'error' ? 'border-bad bg-panel' : 'border-line bg-panel'
          }`}
        >
          <span className={t.level === 'error' ? 'text-bad' : 'text-ink'} style={{ fontSize: 13 }}>
            {t.msg}
          </span>
          <span className="spacer" />
          <button className="!border-0 !p-0 !bg-transparent text-muted"
            aria-label="Dismiss notification" onClick={() => onDismiss(t.id)}>✕</button>
        </div>
      ))}
    </div>
  )
}
