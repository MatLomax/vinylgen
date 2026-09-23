// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConfirmDialog } from './ConfirmDialog'

afterEach(cleanup)

function renderDialog(busy = false) {
  const onConfirm = vi.fn(), onCancel = vi.fn()
  render(<ConfirmDialog open title="Delete project?" message="Gone for good." confirmLabel="Delete project" busy={busy} onConfirm={onConfirm} onCancel={onCancel} />)
  return { onConfirm, onCancel }
}

describe('ConfirmDialog', () => {
  it('focuses Cancel, traps Tab, and cancels on Escape or the backdrop', () => {
    const { onCancel, onConfirm } = renderDialog()
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    const confirm = screen.getByRole('button', { name: 'Delete project' })
    expect(document.activeElement).toBe(cancel)
    confirm.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(cancel)
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(confirm)
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(screen.getByRole('alertdialog').parentElement!)
    expect(onCancel).toHaveBeenCalledTimes(2)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('cannot be dismissed while the confirmed action runs', () => {
    const { onCancel } = renderDialog(true)
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(screen.getByRole('alertdialog').parentElement!)
    expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Delete project' }) as HTMLButtonElement).disabled).toBe(true)
    expect(onCancel).not.toHaveBeenCalled()
    const panel = screen.getByRole('alertdialog')
    expect(document.activeElement).toBe(panel)
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(false)
  })
})
