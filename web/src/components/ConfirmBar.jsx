// Shared in-app confirmation UI, replacing window.confirm(). Rendered once at
// the app root; App.jsx owns the single pending-confirmation slot (only one
// confirmation should ever be visible at a time) and exposes `requestConfirm`
// through context for any panel to await.
export default function ConfirmBar({ pending, onResolve }) {
  if (!pending) return null

  return (
    <div className="fixed inset-x-0 bottom-0 z-50 flex justify-center px-4 pb-4">
      <div className="flex max-w-lg flex-wrap items-center gap-3 rounded-card border border-line bg-panel p-3 shadow-md">
        <span className="text-ink">{pending.message}</span>
        <span className="spacer" />
        <button className="primary" onClick={() => onResolve(true)}>Yes</button>
        <button onClick={() => onResolve(false)}>Cancel</button>
      </div>
    </div>
  )
}
