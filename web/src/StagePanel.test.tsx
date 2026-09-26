// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { StagePanel } from './StagePanel'
import type { PanelMode } from './shellLayout'

afterEach(cleanup)

type PeekAction = { label: string; disabled: boolean; run: () => void; describedBy?: string }
function Panel({ mode, initiallyOpen = false, onChange, peekAction }: { mode: PanelMode; initiallyOpen?: boolean; onChange?: (open: boolean) => void; peekAction?: PeekAction }) {
  const [open, setOpen] = useState(initiallyOpen)
  return <StagePanel mode={mode} open={open} onOpenChange={next => { onChange?.(next); setOpen(next) }} stageLabel="Trace" peekAction={peekAction}>
    <label>Tolerance<input /></label>
  </StagePanel>
}

const field = () => screen.queryByRole('textbox', { name: 'Tolerance' })

describe('StagePanel beside or over the canvas', () => {
  it('closes to a thin edge whose Show button reopens it, moving focus to the toggle that replaced the one pressed', () => {
    render(<Panel mode="push" initiallyOpen />)
    expect(screen.getByRole('complementary', { name: 'Stage settings' })).toBeTruthy()
    expect(screen.getByText('Trace')).toBeTruthy()
    const hide = screen.getByRole('button', { name: 'Hide stage settings' })
    expect(hide.getAttribute('aria-expanded')).toBe('true')
    expect(document.getElementById(hide.getAttribute('aria-controls')!)?.contains(field())).toBe(true)
    fireEvent.click(hide)
    // Closed, the contents stay mounted but hidden, so unsaved input survives.
    expect(field()).toBeNull()
    expect(screen.getByRole('textbox', { name: 'Tolerance', hidden: true })).toBeTruthy()
    const show = screen.getByRole('button', { name: 'Show stage settings' })
    expect(show.getAttribute('aria-expanded')).toBe('false')
    expect(show.getAttribute('title')).toBe('Show Trace settings')
    expect(document.activeElement).toBe(show)
    fireEvent.click(show)
    expect(field()).toBeTruthy()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Hide stage settings' }))
  })

  it('closes an overlay on Escape, returning focus to its Show button', () => {
    render(<Panel mode="overlay" initiallyOpen />)
    fireEvent.keyDown(field()!, { key: 'Escape' })
    expect(field()).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Show stage settings' }))
  })

  it('leaves Escape to the page beside the canvas, and to anything inside that already handled it', () => {
    const onChange = vi.fn()
    const { unmount } = render(<Panel mode="push" initiallyOpen onChange={onChange} />)
    fireEvent.keyDown(field()!, { key: 'Escape' })
    expect(onChange).not.toHaveBeenCalled()
    unmount()
    render(<Panel mode="overlay" initiallyOpen onChange={onChange} />)
    const handled = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    handled.preventDefault()
    field()!.dispatchEvent(handled)
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('StagePanel as a phone drawer', () => {
  const handle = () => screen.getByRole('button', { name: /Trace settings/ })

  it('collapses to its handle, which a tap opens and closes', () => {
    render(<Panel mode="drawer" />)
    expect(handle().getAttribute('aria-expanded')).toBe('false')
    expect(field()).toBeNull()
    fireEvent.click(handle())
    expect(handle().getAttribute('aria-expanded')).toBe('true')
    expect(field()).toBeTruthy()
    fireEvent.click(handle())
    expect(field()).toBeNull()
  })

  it('opens on an upward drag and closes on a downward one, without the drag also toggling as a tap', () => {
    render(<Panel mode="drawer" />)
    const drag = (from: number, to: number) => {
      fireEvent.pointerDown(handle(), { pointerId: 1, clientY: from })
      fireEvent.pointerMove(handle(), { pointerId: 1, clientY: to })
      fireEvent.pointerUp(handle(), { pointerId: 1, clientY: to })
      // A mouse sends its click; a touch drag sends none.
      fireEvent.click(handle(), { detail: 1 })
    }
    drag(600, 400)
    expect(field()).toBeTruthy()
    drag(400, 420)
    expect(field()).toBeNull()
    // A drag the wrong way leaves it as it is.
    drag(600, 700)
    expect(field()).toBeNull()
    // A press that barely moves is a tap.
    drag(600, 596)
    expect(field()).toBeTruthy()
  })

  it('offers the stage\'s run button beside the collapsed handle, leaving it to the settings once open', () => {
    const run = vi.fn()
    render(<Panel mode="drawer" peekAction={{ label: 'Trace and validate', disabled: false, run }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Trace and validate' }))
    expect(run).toHaveBeenCalledOnce()
    expect(field()).toBeNull()
    fireEvent.click(handle())
    expect(screen.queryByRole('button', { name: 'Trace and validate' })).toBeNull()
    cleanup()
    render(<Panel mode="drawer" peekAction={{ label: 'Trace and validate', disabled: true, run }} />)
    expect((screen.getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('offers no run button beside or over the canvas', () => {
    render(<Panel mode="overlay" peekAction={{ label: 'Trace and validate', disabled: false, run: vi.fn() }} />)
    expect(screen.queryByRole('button', { name: 'Trace and validate' })).toBeNull()
  })

  it('still toggles on a keyboard or assistive-technology click right after a touch drag', () => {
    render(<Panel mode="drawer" />)
    fireEvent.pointerDown(handle(), { pointerId: 1, clientY: 600 })
    fireEvent.pointerMove(handle(), { pointerId: 1, clientY: 400 })
    fireEvent.pointerUp(handle(), { pointerId: 1, clientY: 400 })
    expect(field()).toBeTruthy()
    fireEvent.click(handle(), { detail: 0 })
    expect(field()).toBeNull()
  })

  it('points its run button at the reasons it is locked', () => {
    render(<><p id="lock">Choose a project first.</p><Panel mode="drawer" peekAction={{ label: 'Trace and validate', disabled: true, run: vi.fn(), describedBy: 'lock' }} /></>)
    expect(screen.getByRole('button', { name: 'Trace and validate' }).getAttribute('aria-describedby')).toBe('lock')
  })

  it('collapses on Escape, keeping focus on the handle', () => {
    render(<Panel mode="drawer" initiallyOpen />)
    fireEvent.keyDown(field()!, { key: 'Escape' })
    expect(field()).toBeNull()
    expect(document.activeElement).toBe(handle())
  })
})

describe('StagePanel as a whole phone surface', () => {
  it('is always open, with no toggle', () => {
    render(<Panel mode="page" />)
    expect(field()).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
  })
})
