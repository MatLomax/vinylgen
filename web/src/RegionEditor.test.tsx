// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RegionEditor } from './RegionEditor'
import { type PaletteColour } from './api'

const palette: PaletteColour[] = [
  { index: 0, name: 'Night', hex: '#000000', rgb: [0, 0, 0], share_percent: 60 },
  { index: 1, name: 'Gold', hex: '#ffaa00', rgb: [255, 170, 0], share_percent: 30 },
  { index: 2, name: 'Rust', hex: '#aa3300', rgb: [170, 51, 0], share_percent: 10 },
]

function props(overrides: Partial<Parameters<typeof RegionEditor>[0]> = {}) {
  return {
    palette, selectionLabels: [], neighbourLabels: [], onPick: vi.fn(), onClear: vi.fn(),
    onUndo: vi.fn(), canUndo: false, busy: false, ...overrides,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('RegionEditor', () => {
  it('disables every swatch and the clear button while nothing is selected', () => {
    render(<RegionEditor {...props()} />)
    expect(screen.getByText('No areas selected yet.')).toBeTruthy()
    for (const colour of palette) {
      expect(screen.getByRole('button', { name: new RegExp(colour.name) })).toHaveProperty('disabled', true)
    }
    expect(screen.getByRole('button', { name: 'Clear selection' })).toHaveProperty('disabled', true)
  })

  it('applies the picked colour and frames a neighbouring colour as a dissolve', () => {
    const onPick = vi.fn()
    // One area selected using colour 0 (Night); colour 1 (Gold) borders it.
    render(<RegionEditor {...props({ selectionLabels: [0], neighbourLabels: [1], onPick })} />)
    expect(screen.getByText('1 area selected.')).toBeTruthy()

    const gold = screen.getByRole('button', { name: /Dissolve selection into Gold/ })
    fireEvent.click(gold)
    expect(onPick).toHaveBeenCalledWith(1)

    // A non-neighbouring colour recolours instead.
    expect(screen.getByRole('button', { name: /Recolour selection to Rust/ })).toBeTruthy()
    // The selection's own colour is a no-op and stays disabled.
    expect(screen.getByRole('button', { name: /Night/ })).toHaveProperty('disabled', true)
  })

  it('enables Undo only when a parent revision exists', () => {
    const onUndo = vi.fn()
    const { rerender } = render(<RegionEditor {...props({ canUndo: false, onUndo })} />)
    expect(screen.getByRole('button', { name: 'Undo' })).toHaveProperty('disabled', true)
    rerender(<RegionEditor {...props({ canUndo: true, onUndo })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(onUndo).toHaveBeenCalled()
  })

  it('clears the selection', () => {
    const onClear = vi.fn()
    render(<RegionEditor {...props({ selectionLabels: [0, 1], onClear })} />)
    expect(screen.getByText('2 areas selected.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }))
    expect(onClear).toHaveBeenCalled()
  })
})
