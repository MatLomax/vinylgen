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

describe('layer order control', () => {
  it('hides the layer order control while colours meet edge to edge (no solid underlayers, no overlap)', () => {
    renderTrace({ ...DEFAULT_TRACE_SETTINGS, solid_underlayers: false, overlap: 0 }, () => undefined)
    expect(screen.queryByText(/Layer order/)).toBeNull()
  })

  it('shows the layer order control with an overlap and no solid underlayers', () => {
    renderTrace({ ...DEFAULT_TRACE_SETTINGS, solid_underlayers: false, overlap: 0.5 }, () => undefined)
    expect(screen.getByText(/Layer order/)).toBeTruthy()
    expect(screen.getByText('Red')).toBeTruthy()
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
  const fittingLabels = ['Curve-fit tolerance (px)', 'Straight-line deviation (px)', 'Minimum straight length (px)', 'Corner analysis span (px)', 'Maximum deviation (px)']

  it('labels the overlap in pixels without a physical size from Cleanup, and fitting settings in pixels', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    for (const label of ['Layer overlap (px)', ...fittingLabels]) expect(screen.getByText(label)).toBeTruthy()
    expect(screen.getByText('No physical size (px)')).toBeTruthy()
  })

  it('switches the overlap to millimetres when Cleanup set a size, leaving fitting labels in pixels', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined, COLOURS, { width: 300, height: 200 })
    for (const label of ['Layer overlap (mm)', ...fittingLabels]) expect(screen.getByText(label)).toBeTruthy()
    expect(screen.queryByText('Layer overlap (px)')).toBeNull()
    expect(screen.getByText('Size: 300 × 200 mm (set in Cleanup)')).toBeTruthy()
  })

  it('shows the size read-only and no longer offers the cleanup, size or removed-colour fields', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined, COLOURS, { width: 300, height: 200 })
    for (const label of [/Width \(mm\)/, /Height \(mm\)/, /Remove colour/, /Cleanup action/, /Minimum detail width/, /Minimum region area/, /Remove below width/])
      expect(screen.queryByLabelText(label)).toBeNull()
    expect(screen.queryByRole('checkbox', { name: /Enable cutter cleanup/ })).toBeNull()
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
    expect(screen.getByText(/Blank both to keep overlap and cleanup in px\./)).toBeTruthy()
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
    expect(normalizeTraceSettings(stored as Partial<TraceSettings>)).toEqual({ overlap: 2 })
    expect(legacyCleanupSettings(stored)).toEqual({ width_mm: 400, height_mm: null, remove_colour: 'Sand', cleanup: true, cleanup_action: 'widen', min_detail_width: .5, min_region_area: 1, remove_below_width: 0 })
    expect(legacyCleanupSettings({ overlap: 2 })).toBeUndefined()
  })

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
