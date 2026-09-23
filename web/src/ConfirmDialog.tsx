import { useEffect, useId, useRef } from 'react'

// A modal confirmation for irreversible actions: Cancel takes initial focus so a
// stray Enter never confirms, Escape and the backdrop cancel, and Tab stays inside.
export function ConfirmDialog({ open, title, message, confirmLabel, busy = false, onConfirm, onCancel }: {
  open: boolean
  title: string
  message: string
  confirmLabel: string
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const titleId = useId()
  const messageId = useId()
  const panel = useRef<HTMLDivElement>(null)
  const cancelButton = useRef<HTMLButtonElement>(null)
  // Dismissal is ignored while the confirmed action is still running.
  const cancel = useRef(onCancel)
  cancel.current = busy ? () => undefined : onCancel

  useEffect(() => {
    if (!open) return
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    cancelButton.current?.focus()
    const keyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); cancel.current(); return }
      if (event.key !== 'Tab' || !panel.current) return
      const focusable = [...panel.current.querySelectorAll<HTMLElement>('button:not(:disabled)')]
      const first = focusable[0], last = focusable.at(-1)
      if (!first || !last) { event.preventDefault(); return }
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', keyDown)
    return () => {
      document.removeEventListener('keydown', keyDown)
      if (opener?.isConnected) opener.focus()
    }
  }, [open])

  useEffect(() => {
    // Disabling the buttons while busy drops focus to the body; hold it on the panel.
    if (open && busy) panel.current?.focus()
  }, [open, busy])

  if (!open) return null
  return <div className="upscale-modal-shell confirm-modal-shell" onClick={event => { if (event.target === event.currentTarget && !busy) onCancel() }}>
    <div ref={panel} tabIndex={-1} className="upscale-modal confirm-modal" role="alertdialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={messageId}>
      <h2 id={titleId}>{title}</h2>
      <p id={messageId} className="section-intro">{message}</p>
      <div className="upscale-modal-actions">
        <button ref={cancelButton} type="button" className="secondary" disabled={busy} onClick={onCancel}>Cancel</button>
        <button type="button" className="danger" disabled={busy} onClick={onConfirm}>{confirmLabel}</button>
      </div>
    </div>
  </div>
}
