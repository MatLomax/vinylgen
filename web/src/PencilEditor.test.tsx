// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PencilEditor } from './PencilEditor'
import { type PaletteColour } from './api'

const palette: PaletteColour[] = [
  { index: 0, name: 'Night', hex: '#000000', rgb: [0, 0, 0], share_percent: 60 },
  { index: 1, name: 'Gold', hex: '#ffaa00', rgb: [255, 170, 0], share_percent: 30 },
  { index: 2, name: 'Rust', hex: '#aa3300', rgb: [170, 51, 0], share_percent: 10 },
]

function props(overrides: Partial<Parameters<typeof PencilEditor>[0]> = {}) {
  return {
    palette, activeLabel: 0, onPickColour: vi.fn(), paintedCount: 0, onApply: vi.fn(),
    onDiscard: vi.fn(), onUndo: vi.fn(), canUndo: false, busy: false, ...overrides,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('PencilEditor', () => {
  it('marks the active pencil colour and disables Apply until pixels are painted', () => {
    render(<PencilEditor {...props({ activeLabel: 1 })} />)
    expect(screen.getByText('No pixels painted yet.')).toBeTruthy()
    expect(screen.getByRole('radio', { name: /Gold/ }).getAttribute('aria-checked')).toBe('true')
    expect(screen.getByRole('radio', { name: /Night/ }).getAttribute('aria-checked')).toBe('false')
    expect(screen.getByRole('button', { name: 'Apply pencil edits' })).toHaveProperty('disabled', true)
    expect(screen.getByRole('button', { name: 'Discard' })).toHaveProperty('disabled', true)
  })

  it('picks a pencil colour', () => {
    const onPickColour = vi.fn()
    render(<PencilEditor {...props({ onPickColour })} />)
    fireEvent.click(screen.getByRole('radio', { name: /Paint with Rust/ }))
    expect(onPickColour).toHaveBeenCalledWith(2)
  })

  it('applies and discards staged strokes once pixels are painted', () => {
    const onApply = vi.fn(), onDiscard = vi.fn()
    render(<PencilEditor {...props({ paintedCount: 5, onApply, onDiscard })} />)
    expect(screen.getByText('5 pixels painted, not yet applied.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Apply pencil edits' }))
    expect(onApply).toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(onDiscard).toHaveBeenCalled()
  })

  it('enables Undo only when a parent revision exists', () => {
    const onUndo = vi.fn()
    const { rerender } = render(<PencilEditor {...props({ canUndo: false, onUndo })} />)
    expect(screen.getByRole('button', { name: 'Undo' })).toHaveProperty('disabled', true)
    rerender(<PencilEditor {...props({ canUndo: true, onUndo })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(onUndo).toHaveBeenCalled()
  })

  it('locks every control and shows the hint when the wrong image is active', () => {
    render(<PencilEditor {...props({ paintedCount: 3, disabled: true, disabledHint: 'Switch to the Edited image to paint pixels.' })} />)
    expect(screen.getByText('Switch to the Edited image to paint pixels.')).toBeTruthy()
    expect(screen.getByRole('radio', { name: /Night/ })).toHaveProperty('disabled', true)
    expect(screen.getByRole('button', { name: 'Apply pencil edits' })).toHaveProperty('disabled', true)
  })
})
