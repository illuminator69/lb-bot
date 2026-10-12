// Toast stack for things the user needs to notice: action results and errors.
// All of them expire on their own (App.jsx's pushToast); an error stays about
// twice as long and carries a button to dismiss it sooner.
export default function Toasts({ toasts, onDismiss }) {
  if (!toasts.length) return null
  return (
    <div className="toasts" role="status" aria-live="polite" aria-atomic="false">
      {toasts.map(t => (
        <div key={t.id} className={`toast${t.level === 'error' ? ' error' : ''}`}>
          <span>{t.msg}</span>
          {t.level === 'error' && (
            <button aria-label="Dismiss notification" onClick={() => onDismiss(t.id)}>✕</button>
          )}
        </div>
      ))}
    </div>
  )
}
