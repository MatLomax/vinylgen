// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { TraceSettings } from './api'
import { DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS, PipelineControls } from './PipelineControls'

afterEach(cleanup)

describe('numeric field empty handling', () => {
  it('retains the last valid value when a required field is cleared to empty', () => {
    const setPalette = vi.fn()
    render(<PipelineControls stage="flatten" palette={DEFAULT_PALETTE_SETTINGS} setPalette={setPalette} basePalette={DEFAULT_PALETTE_SETTINGS} trace={DEFAULT_TRACE_SETTINGS} setTrace={() => undefined} baseTrace={DEFAULT_TRACE_SETTINGS}
      selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} onRestoreLast={() => undefined} onCommitDraft={() => undefined} />)

    fireEvent.change(screen.getByLabelText(/Initial candidates/), { target: { value: '' } })
    expect(setPalette).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText(/Initial candidates/), { target: { value: '5' } })
    expect(setPalette).toHaveBeenCalledWith(expect.objectContaining({ initial_colours: 5 }))
  })

  it('clears an optional field to null when emptied', () => {
    const setTrace = vi.fn()
    const trace: TraceSettings = { ...DEFAULT_TRACE_SETTINGS, width_mm: 120 }
    render(<PipelineControls stage="trace" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={trace} setTrace={setTrace} baseTrace={DEFAULT_TRACE_SETTINGS}
      selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} onRestoreLast={() => undefined} onCommitDraft={() => undefined} />)

    fireEvent.change(screen.getByLabelText(/Width \(mm\)/), { target: { value: '' } })
    expect(setTrace).toHaveBeenCalledWith(expect.objectContaining({ width_mm: null }))
  })
})
