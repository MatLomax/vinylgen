// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CleanupSettings, PaletteColour, TraceSettings } from './api'
import { DEFAULT_CLEANUP_SETTINGS, DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS, PipelineControls, formatPhysicalSize, paletteColourMatches, removedColourChoice, legacyCleanupSettings, normalizeTraceSettings, physicalSize, type PhysicalSize } from './PipelineControls'

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
    const setCleanup = vi.fn()
    renderCleanup({ ...DEFAULT_CLEANUP_SETTINGS, width_mm: 120 }, setCleanup)
    fireEvent.change(screen.getByLabelText(/Width \(mm\)/), { target: { value: '' } })
    expect(setCleanup).toHaveBeenCalledWith(expect.objectContaining({ width_mm: null }))
  })
})

function renderTrace(trace: TraceSettings, setTrace: (value: TraceSettings) => void, colours = COLOURS, traceSize: PhysicalSize | null = null) {
  render(<PipelineControls stage="trace" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={trace} setTrace={setTrace} baseTrace={DEFAULT_TRACE_SETTINGS} paletteColours={colours}
    selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} traceSize={traceSize} />)
}
function renderCleanup(cleanup: CleanupSettings, setCleanup: (value: CleanupSettings) => void, baseCleanup = DEFAULT_CLEANUP_SETTINGS, colours = COLOURS, removalPaletteCurrent = true) {
  render(<PipelineControls stage="cleanup" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={DEFAULT_TRACE_SETTINGS} setTrace={() => undefined} baseTrace={DEFAULT_TRACE_SETTINGS}
    cleanup={cleanup} setCleanup={setCleanup} baseCleanup={baseCleanup} paletteColours={colours} removalPaletteCurrent={removalPaletteCurrent}
    selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} />)
}

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

describe('trace form', () => {
  it('states the physical size from Cleanup read-only, in px without one', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    expect(screen.getByText('No physical size (px)')).toBeTruthy()
    cleanup()
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined, COLOURS, { width: 300, height: 200 })
    expect(screen.getByText('Size: 300 × 200 mm (set in Cleanup)')).toBeTruthy()
  })

  it('shows the size read-only and no longer offers the cleanup, size or removed-colour fields', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined, COLOURS, { width: 300, height: 200 })
    for (const label of [/Width \(mm\)/, /Height \(mm\)/, /Remove colour/, /Cleanup action/, /Minimum detail width/, /Minimum region area/, /Remove below width/])
      expect(screen.queryByLabelText(label)).toBeNull()
    expect(screen.queryByRole('checkbox', { name: /Enable cutter cleanup/ })).toBeNull()
  })

  it('states the traced image size only when it is known', () => {
    render(<PipelineControls stage="trace" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={DEFAULT_TRACE_SETTINGS} setTrace={() => undefined} baseTrace={DEFAULT_TRACE_SETTINGS} paletteColours={[]}
      selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} tracedSize={{ width: 4344, height: 1448 }} />)
    expect(screen.getByText('Traced image: 4344 × 1448 px')).toBeTruthy()
    cleanup()
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    expect(screen.queryByText(/Traced image:/)).toBeNull()
  })

  it('edits a soft limit under its own key', () => {
    const setTrace = vi.fn()
    renderTrace(DEFAULT_TRACE_SETTINGS, setTrace)
    fireEvent.change(screen.getByLabelText(/Nodes per path/), { target: { value: '800' } })
    expect(setTrace).toHaveBeenCalledWith({ ...DEFAULT_TRACE_SETTINGS, soft_path_nodes: 800 })
  })
})

describe('cleanup settings form', () => {
  const physicalLabels = (unit: string, areaUnit: string) => [
    `Minimum detail width (${unit})`, `Remove below width (${unit})`, `Minimum region area (${areaUnit})`]

  it('holds the size, removed colour and cutter cleanup, labelled in px without a size', () => {
    renderCleanup(DEFAULT_CLEANUP_SETTINGS, () => undefined)
    for (const label of ['Width (mm)', 'Height (mm)', 'Remove colour', 'Cleanup action', ...physicalLabels('px', 'px²')]) expect(screen.getByText(label)).toBeTruthy()
    expect(screen.getByRole('checkbox', { name: /Enable cutter cleanup/ })).toBeTruthy()
    // The trace-only settings stay on Trace.
    expect(screen.queryByText(/Layer overlap/)).toBeNull()
    expect(screen.queryByText(/Curve-fit tolerance/)).toBeNull()
    expect(screen.getByText(/Blank both to keep cleanup in px\./)).toBeTruthy()
    expect(screen.getByText(/Off: the run only records the size and removed colour/)).toBeTruthy()
  })

  it('switches its physical labels to millimetres when a width or height is set', () => {
    for (const size of [{ width_mm: 120 }, { height_mm: 80 }]) {
      renderCleanup({ ...DEFAULT_CLEANUP_SETTINGS, ...size }, () => undefined)
      for (const label of physicalLabels('mm', 'mm²')) expect(screen.getByText(label)).toBeTruthy()
      cleanup()
    }
  })

  it('edits its own keys and flags fields that differ from the recorded cleanup', () => {
    const setCleanup = vi.fn()
    renderCleanup({ ...DEFAULT_CLEANUP_SETTINGS, remove_colour: 'Red' }, setCleanup)
    const colour = screen.getByLabelText(/Remove colour/)
    expect(colour.closest('label')?.querySelector('.field-dirty')).toBeTruthy()
    expect(screen.getByLabelText(/Width \(mm\)/).closest('label')?.querySelector('.field-dirty')).toBeNull()
    fireEvent.click(screen.getByRole('checkbox', { name: /Enable cutter cleanup/ }))
    expect(setCleanup).toHaveBeenCalledWith(expect.objectContaining({ cleanup: true, remove_colour: 'Red' }))
    fireEvent.change(screen.getByLabelText(/Cleanup action/), { target: { value: 'widen' } })
    expect(setCleanup).toHaveBeenCalledWith(expect.objectContaining({ cleanup_action: 'widen' }))
  })

  describe('removed colour, resolved as the server resolves it', () => {
    const colour = (index: number, name: string, hex: string): PaletteColour => ({ index, name, hex, rgb: [0, 0, 0], share_percent: 10 })
    const note = () => document.querySelector('#remove-colour-status')?.textContent ?? null

    it('matches by name or hex, case-insensitively, and reports more than one match as ambiguous', () => {
      // "Facade" is a name that reads as hex: it matches the colour so named and #FACADE.
      const palette = [colour(0, 'Facade', '#112233'), colour(1, 'Stone', '#FACADE'), colour(2, '#E15F2D', '#445566'), colour(3, 'Ember', '#E15F2D')]
      expect(paletteColourMatches('facade', palette).map(row => row.index)).toEqual([0, 1])
      expect(removedColourChoice('Facade', palette).status).toBe('ambiguous')
      // A colour named like another's hex makes that hex ambiguous too.
      expect(removedColourChoice('#e15f2d', palette).status).toBe('ambiguous')
      expect(removedColourChoice('  stone ', palette)).toEqual({ status: 'match', colour: palette[1] })
      expect(removedColourChoice('fACADE', [colour(0, 'Stone', '#FACADE')])).toEqual({ status: 'match', colour: expect.objectContaining({ name: 'Stone' }) })
      expect(removedColourChoice('Facade', [colour(0, 'Facade', '#112233')]).colour?.hex).toBe('#112233')
      expect(removedColourChoice('Nope', palette).status).toBe('missing')
      // Case folding as Python's casefold: "ß" folds to "ss".
      expect(removedColourChoice('STRASSE', [colour(0, 'Straße', '#010203')]).status).toBe('match')
    })

    it('shows the ambiguity as an alert beside, not inside, the field', () => {
      renderCleanup({ ...DEFAULT_CLEANUP_SETTINGS, remove_colour: 'Facade' }, () => undefined, DEFAULT_CLEANUP_SETTINGS, [colour(0, 'Facade', '#112233'), colour(1, 'Stone', '#FACADE')])
      const alert = screen.getByRole('alert')
      expect(alert.textContent).toBe("Facade names more than one colour of this artwork's palette, by name or hex: choose the colour to remove.")
      expect(alert.closest('label')).toBeNull()
      const input = screen.getByLabelText(/Remove colour/)
      expect(input.getAttribute('aria-describedby')).toBe('remove-colour-status')
      expect(input.getAttribute('aria-invalid')).toBe('true')
    })

    it('judges nothing until the palette listed is the shown revision\'s', () => {
      renderCleanup({ ...DEFAULT_CLEANUP_SETTINGS, remove_colour: '#E15F2D' }, () => undefined, DEFAULT_CLEANUP_SETTINGS, COLOURS, false)
      expect(screen.queryByRole('alert')).toBeNull()
      expect(note()).toBeNull()
      expect(screen.getByLabelText(/Remove colour/).getAttribute('aria-invalid')).toBeNull()
      cleanup()
      renderCleanup({ ...DEFAULT_CLEANUP_SETTINGS, remove_colour: '#E15F2D' }, () => undefined)
      expect(screen.getByRole('alert').textContent).toBe("No colour of this artwork's palette is #E15F2D: choose the colour to remove.")
    })
  })

  it('steps its settings history like Flatten', () => {
    const onStep = vi.fn()
    render(<PipelineControls stage="cleanup" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={DEFAULT_TRACE_SETTINGS} setTrace={() => undefined} baseTrace={DEFAULT_TRACE_SETTINGS} paletteColours={[]}
      selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={2} iterationIndex={0} onStep={onStep} onCommitDraft={() => undefined} />)
    expect(screen.getByRole('group', { name: 'Step through previous iterations' })).toBeTruthy()
    fireEvent.click(screen.getByLabelText("Load an older iteration's settings"))
    expect(onStep).toHaveBeenCalledWith(1)
  })
})

describe('physical size', () => {
  it('completes a one-sided size from the raster proportions', () => {
    expect(physicalSize({ width_mm: 300, height_mm: null }, { width: 600, height: 400 })).toEqual({ width: 300, height: 200 })
    expect(physicalSize({ width_mm: null, height_mm: 100 }, { width: 600, height: 400 })).toEqual({ width: 150, height: 100 })
    expect(physicalSize({ width_mm: 300, height_mm: null }, null)).toEqual({ width: 300, height: null })
    expect(physicalSize({ width_mm: null, height_mm: null }, { width: 600, height: 400 })).toBeNull()
    expect(physicalSize(undefined, null)).toBeNull()
  })

  it('reads as the trace panel states it', () => {
    expect(formatPhysicalSize(null)).toBe('No physical size (px)')
    expect(formatPhysicalSize({ width: 300, height: 213.3333 })).toBe('Size: 300 × 213.33 mm (set in Cleanup)')
    expect(formatPhysicalSize({ width: 300, height: null })).toBe('Size: 300 mm wide (set in Cleanup)')
  })
})

describe('legacy trace settings', () => {
  it('moves embedded size, removed colour and cleanup out of the trace form', () => {
    const stored = { overlap: 2, width_mm: 400, height_mm: null, remove_colour: 'Sand', cleanup: true, cleanup_action: 'widen', min_detail_width: .5, min_region_area: 1, remove_below_width: 0 }
    expect(normalizeTraceSettings(stored)).toEqual({})
    expect(legacyCleanupSettings(stored)).toEqual({ width_mm: 400, height_mm: null, remove_colour: 'Sand', cleanup: true, cleanup_action: 'widen', min_detail_width: .5, min_region_area: 1, remove_below_width: 0 })
    expect(legacyCleanupSettings({ overlap: 2 })).toBeUndefined()
  })

  it('keeps only the three soft limits of a stored record or draft, whatever else it carries', () => {
    // A pixel-era record, a thousandths record and a layered record alike: the old
    // fitter's fields, the overlap and the layer order are retired with it.
    const stored = { curve_fit_tolerance: .9, straight_line_max_deviation: 1.6, curve_optimization: false, length_unit: 'px', overlap: 2,
      solid_underlayers: true, layer_order: ['#ff0000'], corner_threshold: 45, soft_path_nodes: 900, soft_document_nodes: 1000 }
    expect(normalizeTraceSettings(stored)).toEqual({ soft_path_nodes: 900, soft_document_nodes: 1000 })
    expect({ ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings(stored) }).toEqual({ soft_path_nodes: 900, soft_layer_nodes: 50000, soft_document_nodes: 1000 })
  })
})
