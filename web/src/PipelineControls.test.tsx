// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PaletteColour, TraceSettings } from './api'
import { DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS, PipelineControls, normalizeTraceSettings } from './PipelineControls'

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

describe('trace setting units', () => {
  const physicalLabels = (unit: string, areaUnit: string) => [
    `Layer overlap (${unit})`, `Minimum detail width (${unit})`, `Remove below width (${unit})`, `Minimum region area (${areaUnit})`]
  const fittingLabels = ['Curve-fit tolerance (px)', 'Straight-line deviation (px)', 'Minimum straight length (px)', 'Corner analysis span (px)', 'Maximum deviation (px)']

  it('labels physical settings in pixels without a physical size, and fitting settings in pixels', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    for (const label of [...physicalLabels('px', 'px²'), ...fittingLabels]) expect(screen.getByText(label)).toBeTruthy()
  })

  it('switches physical labels to millimetres when a width or height is set, leaving fitting labels in pixels', () => {
    for (const size of [{ width_mm: 120 }, { height_mm: 80 }]) {
      renderTrace({ ...DEFAULT_TRACE_SETTINGS, ...size }, () => undefined)
      for (const label of [...physicalLabels('mm', 'mm²'), ...fittingLabels]) expect(screen.getByText(label)).toBeTruthy()
      expect(screen.queryByText('Layer overlap (px)')).toBeNull()
      cleanup()
    }
  })

  it('offers straight-line deviation as an unbounded number field with no curve optimisation checkbox', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    const field = screen.getByLabelText(/Straight-line deviation/) as HTMLInputElement
    expect(field.type).toBe('number')
    expect(field.min).toBe('0')
    expect(field.max).toBe('')
    expect(screen.getByText(/0: turns this off/)).toBeTruthy()
    expect(screen.queryByText(/Optimize curves/)).toBeNull()
  })

  it('states the traced image size only when it is known', () => {
    render(<PipelineControls stage="trace" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={DEFAULT_TRACE_SETTINGS} setTrace={() => undefined} baseTrace={DEFAULT_TRACE_SETTINGS} paletteColours={[]}
      selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} tracedSize={{ width: 4344, height: 1448 }} />)
    expect(screen.getByText('Traced image: 4344 × 1448 px')).toBeTruthy()
    cleanup()
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    expect(screen.queryByText(/Traced image:/)).toBeNull()
  })
})

describe('legacy trace settings', () => {
  it('maps curve_optimization false to a straight-line deviation of 0 and drops the flag', () => {
    // Corners keep the recorded deviation they were detected with.
    expect(normalizeTraceSettings({ curve_optimization: false, straight_line_max_deviation: 1.6, overlap: 2 })).toEqual({ straight_line_max_deviation: 0, corner_straightness: 1.6, overlap: 2 })
    expect(normalizeTraceSettings({ curve_optimization: true, straight_line_max_deviation: 1.6 })).toEqual({ straight_line_max_deviation: 1.6, corner_straightness: 1.6 })
    expect(normalizeTraceSettings({ overlap: 1 })).toEqual({ overlap: 1 })
  })

  it('maps missing corner straightness to the recorded straight-line deviation', () => {
    expect(normalizeTraceSettings({ straight_line_max_deviation: .7 })).toEqual({ straight_line_max_deviation: .7, corner_straightness: .7 })
    expect(normalizeTraceSettings({ straight_line_max_deviation: .7, corner_straightness: null as unknown as number })).toEqual({ straight_line_max_deviation: .7, corner_straightness: .7 })
    expect(normalizeTraceSettings({ straight_line_max_deviation: 0, corner_straightness: .4 })).toEqual({ straight_line_max_deviation: 0, corner_straightness: .4 })
    // Absent both, merging over the defaults gives the 1.6 px default.
    expect({ ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings({ overlap: 1 }) }.corner_straightness).toBe(1.6)
  })
})

describe('corner straightness control', () => {
  it('renders as an unbounded px number field beside the corner settings and edits its own key', () => {
    const setTrace = vi.fn()
    renderTrace({ ...DEFAULT_TRACE_SETTINGS, corner_straightness: .9 }, setTrace)
    const field = screen.getByLabelText(/Corner straightness \(px\)/) as HTMLInputElement
    expect(field.value).toBe('0.9')
    expect(field.min).toBe('0')
    expect(field.max).toBe('')
    const labels = Array.from(document.querySelectorAll('.setting-field > span')).map(node => node.textContent)
    expect(labels.indexOf('Corner straightness (px)')).toBe(labels.indexOf('Corner analysis span (px)') + 1)
    fireEvent.change(field, { target: { value: '0.3' } })
    expect(setTrace).toHaveBeenCalledWith(expect.objectContaining({ corner_straightness: .3, straight_line_max_deviation: 1.6 }))
  })

  it('describes maximum deviation as a curve-tolerance cap that never limits lines', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    const help = screen.getByLabelText(/Maximum deviation/).closest('label')?.querySelector('small')?.textContent ?? ''
    expect(help).toMatch(/caps the curve-fit tolerance/)
    expect(help).toMatch(/Does not limit straight lines/)
  })

  it('keeps corners out of the straight-line deviation help', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    const help = screen.getByLabelText(/Straight-line deviation/).closest('label')?.querySelector('small')?.textContent ?? ''
    expect(help).toMatch(/0: turns this off, though bends shallower than the curve-fit tolerance can still become lines/)
    expect(help).not.toMatch(/corner/i)
  })
})
