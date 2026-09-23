// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PaletteColour, TraceSettings } from './api'
import { DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS, PipelineControls } from './PipelineControls'

afterEach(cleanup)

const COLOURS: PaletteColour[] = [
  { index: 0, name: 'Red', hex: '#ff0000', rgb: [255, 0, 0], share_percent: 50 },
  { index: 1, name: 'Blue', hex: '#0000ff', rgb: [0, 0, 255], share_percent: 50 },
]

describe('numeric field empty handling', () => {
  it('retains the last valid value when a required field is cleared to empty', () => {
    const setPalette = vi.fn()
    render(<PipelineControls stage="flatten" palette={DEFAULT_PALETTE_SETTINGS} setPalette={setPalette} basePalette={DEFAULT_PALETTE_SETTINGS} trace={DEFAULT_TRACE_SETTINGS} setTrace={() => undefined} baseTrace={DEFAULT_TRACE_SETTINGS} paletteColours={[]}
      selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} />)

    fireEvent.change(screen.getByLabelText(/Initial candidates/), { target: { value: '' } })
    expect(setPalette).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText(/Initial candidates/), { target: { value: '5' } })
    expect(setPalette).toHaveBeenCalledWith(expect.objectContaining({ initial_colours: 5 }))
  })

  it('clears an optional field to null when emptied', () => {
    const setTrace = vi.fn()
    const trace: TraceSettings = { ...DEFAULT_TRACE_SETTINGS, width_mm: 120 }
    render(<PipelineControls stage="trace" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={trace} setTrace={setTrace} baseTrace={DEFAULT_TRACE_SETTINGS} paletteColours={[]}
      selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} />)

    fireEvent.change(screen.getByLabelText(/Width \(mm\)/), { target: { value: '' } })
    expect(setTrace).toHaveBeenCalledWith(expect.objectContaining({ width_mm: null }))
  })
})

function renderTrace(trace: TraceSettings, setTrace: (value: TraceSettings) => void, colours = COLOURS) {
  render(<PipelineControls stage="trace" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={trace} setTrace={setTrace} baseTrace={DEFAULT_TRACE_SETTINGS} paletteColours={colours}
    selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} />)
}

describe('layer order control', () => {
  it('hides the layer order control when solid underlayers is off', () => {
    renderTrace({ ...DEFAULT_TRACE_SETTINGS, solid_underlayers: false }, () => undefined)
    expect(screen.queryByText(/Layer order/)).toBeNull()
  })

  it('shows a reorderable palette list when solid underlayers is on', () => {
    renderTrace({ ...DEFAULT_TRACE_SETTINGS, solid_underlayers: true }, () => undefined)
    expect(screen.getByText(/Layer order/)).toBeTruthy()
    expect(screen.getByText('Red')).toBeTruthy()
    expect(screen.getByText('Blue')).toBeTruthy()
  })

  it('serialises a reorder as bottom-to-top hex values', () => {
    const setTrace = vi.fn()
    // Automatic order: natural palette [Red, Blue] is bottom-to-top, so the list shows Blue on top.
    renderTrace({ ...DEFAULT_TRACE_SETTINGS, solid_underlayers: true, layer_order: null }, setTrace)
    fireEvent.click(screen.getByLabelText(/Move Blue down a layer/))
    expect(setTrace).toHaveBeenCalledWith(expect.objectContaining({ layer_order: ['#0000ff', '#ff0000'] }))
  })

  it('reverts to automatic order', () => {
    const setTrace = vi.fn()
    renderTrace({ ...DEFAULT_TRACE_SETTINGS, solid_underlayers: true, layer_order: ['#0000ff', '#ff0000'] }, setTrace)
    fireEvent.click(screen.getByText(/Reset to automatic/))
    expect(setTrace).toHaveBeenCalledWith(expect.objectContaining({ layer_order: null }))
  })
})

describe('iteration stepper', () => {
  function renderStepper(iterationCount: number, iterationIndex: number) {
    const onStep = vi.fn()
    render(<PipelineControls stage="flatten" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={DEFAULT_TRACE_SETTINGS} setTrace={() => undefined} baseTrace={DEFAULT_TRACE_SETTINGS} paletteColours={[]}
      selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={iterationCount} iterationIndex={iterationIndex} onStep={onStep} onCommitDraft={() => undefined} />)
    return {
      onStep,
      back: screen.getByLabelText("Load an older iteration's settings") as HTMLButtonElement,
      forward: screen.getByLabelText("Load a newer iteration's settings") as HTMLButtonElement,
    }
  }

  it('steps back toward older iterations from the working form, with forward disabled', () => {
    const { onStep, back, forward } = renderStepper(3, 0)
    expect(screen.getByText('Iteration 3 of 3')).toBeTruthy()
    expect(forward.disabled).toBe(true)
    expect(back.disabled).toBe(false)
    fireEvent.click(back)
    expect(onStep).toHaveBeenCalledWith(1)
  })

  it('disables stepping back at the oldest iteration and steps forward otherwise', () => {
    const { onStep, back, forward } = renderStepper(3, 2)
    expect(screen.getByText('Iteration 1 of 3')).toBeTruthy()
    expect(back.disabled).toBe(true)
    expect(forward.disabled).toBe(false)
    fireEvent.click(forward)
    expect(onStep).toHaveBeenCalledWith(-1)
  })

  it('labels trace steps as saved runs and locks them while a job runs', () => {
    const onStep = vi.fn()
    render(<PipelineControls stage="trace" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={DEFAULT_TRACE_SETTINGS} setTrace={() => undefined} baseTrace={DEFAULT_TRACE_SETTINGS} paletteColours={[]}
      selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={3} iterationIndex={1} onStep={onStep} stepDisabled onCommitDraft={() => undefined} />)
    expect(screen.getByRole('group', { name: 'Step through trace runs' })).toBeTruthy()
    expect((screen.getByLabelText('Show the previous trace run') as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByLabelText('Show the next trace run') as HTMLButtonElement).disabled).toBe(true)
  })

  it('disables both arrows and reads no past runs before any iteration exists', () => {
    const { back, forward } = renderStepper(0, 0)
    expect(screen.getByText('No past runs')).toBeTruthy()
    expect(back.disabled).toBe(true)
    expect(forward.disabled).toBe(true)
  })
})
