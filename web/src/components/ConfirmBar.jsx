import { useEffect, useRef } from 'react'

// Shared in-app confirmation, replacing window.confirm(). App.jsx owns the one
// pending slot; `requestConfirm(message, { confirmLabel, danger })` awaits it.
// Escape cancels. A plain confirm takes focus so Enter confirms; a destructive
// one focuses Cancel instead, so an Enter or Space still held from the keypress
// that asked cannot delete a folder or empty the trash.
export default function ConfirmBar({ pending, onResolve }) {
  const okRef = useRef(null)
  const cancelRef = useRef(null)
  useEffect(() => {
    if (!pending) return
    const focusRef = pending.danger ? cancelRef : okRef
    focusRef.current?.focus()
    function onKey(e) { if (e.key === 'Escape') onResolve(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [pending, onResolve])
  if (!pending) return null

  return (
    <div className="fixed inset-x-0 bottom-0 z-50 flex justify-center px-4 pb-4">
      <div role="alertdialog" aria-modal="false" aria-label="Confirm"
        className="flex max-w-lg flex-wrap items-center gap-3 rounded-card border border-line bg-panel p-3.5"
        style={{ boxShadow: '0 20px 48px -14px rgba(0,0,0,.6)' }}>
        <span className="whitespace-pre-line text-small text-ink">{pending.message}</span>
        <span className="spacer" />
        <button ref={okRef} className={pending.danger ? 'danger' : 'primary'} onClick={() => onResolve(true)}>
          {pending.confirmLabel || 'Yes'}
        </button>
        <button ref={cancelRef} onClick={() => onResolve(false)}>Cancel</button>
      </div>
    </div>
  )
}
