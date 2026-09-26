// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CleanupSettings, PaletteColour, TraceSettings } from './api'
import { DEFAULT_CLEANUP_SETTINGS, DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS, PipelineControls, formatPhysicalSize, paletteColourMatches, removedColourChoice, legacyCleanupSettings, normalizeTraceSettings, physicalSize, TRACE_LENGTH_FIELDS, isPixelRecord, lengthInPixels, pixelHint, pixelRecordInUnits, type PhysicalSize, type TraceLengthField } from './PipelineControls'

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
  const fittingLabels = ['Curve-fit tolerance (‰)', 'Straight-line deviation (‰)', 'Minimum straight length (‰)', 'Analysis scale (‰)', 'Maximum deviation (‰)', 'Side smoothness (‰)', 'Corner sharpening allowance (‰)', 'Curve feature scale (‰)']

  it('labels the overlap in pixels without a physical size from Cleanup, and fitting settings in thousandths', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    for (const label of ['Layer overlap (px)', ...fittingLabels]) expect(screen.getByText(label)).toBeTruthy()
    expect(screen.getByText('No physical size (px)')).toBeTruthy()
  })

  it('switches the overlap to millimetres when Cleanup set a size, leaving fitting labels in thousandths', () => {
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
    expect(normalizeTraceSettings({ curve_optimization: false, straight_line_max_deviation: 1.6, overlap: 2 })).toEqual({ straight_line_max_deviation: 0, side_smoothness: 1.6, overlap: 2 })
    expect(normalizeTraceSettings({ curve_optimization: true, straight_line_max_deviation: 1.6 })).toEqual({ straight_line_max_deviation: 1.6, side_smoothness: 1.6 })
    expect(normalizeTraceSettings({ overlap: 1 })).toEqual({ overlap: 1 })
  })

  it('maps missing side smoothness to the recorded straight-line deviation', () => {
    expect(normalizeTraceSettings({ straight_line_max_deviation: .7 })).toEqual({ straight_line_max_deviation: .7, side_smoothness: .7 })
    expect(normalizeTraceSettings({ straight_line_max_deviation: .7, side_smoothness: null as unknown as number })).toEqual({ straight_line_max_deviation: .7, side_smoothness: .7 })
    expect(normalizeTraceSettings({ straight_line_max_deviation: 0, side_smoothness: .4 })).toEqual({ straight_line_max_deviation: 0, side_smoothness: .4 })
    // Absent both, merging over the defaults gives the default (thousandths).
    expect({ ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings({ overlap: 1 }) }.side_smoothness).toBe(.8)
  })

  it('maps a record still keyed corner_straightness onto side_smoothness and drops the old key', () => {
    expect(normalizeTraceSettings({ straight_line_max_deviation: 1.3, corner_straightness: 1.6 })).toEqual({ straight_line_max_deviation: 1.3, side_smoothness: 1.6 })
    // A record naming both (a draft saved mid-migration) keeps the current key.
    expect(normalizeTraceSettings({ straight_line_max_deviation: 1.3, side_smoothness: .9, corner_straightness: 1.6 })).toEqual({ straight_line_max_deviation: 1.3, side_smoothness: .9 })
    expect(normalizeTraceSettings({ straight_line_max_deviation: 1.3, corner_straightness: 1.6 })).not.toHaveProperty('corner_straightness')
  })
})

describe('side smoothness control', () => {
  it('renders as an unbounded number field beside the corner settings and edits its own key', () => {
    const setTrace = vi.fn()
    renderTrace({ ...DEFAULT_TRACE_SETTINGS, side_smoothness: .9 }, setTrace)
    const field = screen.getByLabelText(/Side smoothness \(‰\)/) as HTMLInputElement
    expect(field.value).toBe('0.9')
    expect(field.min).toBe('0')
    expect(field.max).toBe('')
    const labels = Array.from(document.querySelectorAll('.setting-field > span')).map(node => node.textContent)
    expect(labels.indexOf('Side smoothness (‰)')).toBe(labels.indexOf('Analysis scale (‰)') + 1)
    expect(labels.indexOf('Corner sharpening allowance (‰)')).toBe(labels.indexOf('Side smoothness (‰)') + 1)
    fireEvent.change(field, { target: { value: '0.3' } })
    expect(setTrace).toHaveBeenCalledWith(expect.objectContaining({ side_smoothness: .3, straight_line_max_deviation: .8 }))
  })

  it('describes maximum deviation as a cap on the curve-fit tolerance and straight-line deviation that the allowance follows', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    const help = screen.getByLabelText(/Maximum deviation/).closest('label')?.querySelector('small')?.textContent ?? ''
    expect(help).toMatch(/caps the curve-fit tolerance and the straight-line deviation/)
    expect(help).toMatch(/the corner sharpening allowance follows it unless set/)
    expect(help).not.toMatch(/Does not limit straight lines/)
  })

  it('keeps corners out of the straight-line deviation help', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    const help = screen.getByLabelText(/Straight-line deviation/).closest('label')?.querySelector('small')?.textContent ?? ''
    expect(help).toMatch(/0: turns this off, though bends shallower than the curve-fit tolerance can still become lines/)
    expect(help).not.toMatch(/corner/i)
  })
})

describe('corner sharpening allowance control', () => {
  it('renders as an optional field beside side smoothness and edits its own key', () => {
    const setTrace = vi.fn()
    renderTrace({ ...DEFAULT_TRACE_SETTINGS, corner_sharpening_allowance: .6 }, setTrace)
    const field = screen.getByLabelText(/Corner sharpening allowance \(‰\)/) as HTMLInputElement
    expect(field.value).toBe('0.6')
    expect(field.min).toBe('0.0001')
    fireEvent.change(field, { target: { value: '' } })
    expect(setTrace).toHaveBeenCalledWith(expect.objectContaining({ corner_sharpening_allowance: null }))
  })

  it('is null (automatic) by default and shows the Automatic placeholder when empty', () => {
    expect(DEFAULT_TRACE_SETTINGS.corner_sharpening_allowance).toBeNull()
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    const field = screen.getByLabelText(/Corner sharpening allowance \(‰\)/) as HTMLInputElement
    expect(field.value).toBe('')
    expect(field.placeholder).toBe('Automatic')
  })

  it('describes what it does and that it follows the maximum deviation automatically', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    const help = screen.getByLabelText(/Corner sharpening allowance/).closest('label')?.querySelector('small')?.textContent ?? ''
    expect(help).toMatch(/Automatic: the maximum deviation/)
    expect(help).toMatch(/2\.4 times it snap sharp/)
  })
})

describe('node placement and straight-line snapping', () => {
  it('defaults to fewest nodes preferring extremes, bends on, a 2 ‰ scale and horizontal & vertical snapping at 2 degrees', () => {
    expect(DEFAULT_TRACE_SETTINGS).toMatchObject({ node_placement: 'prefer_extremes', nodes_at_bends: true, curve_feature_scale: 2, straight_line_snap: 'axis', snap_angle: 2 })
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    const placement = screen.getByLabelText(/^Node placement/) as HTMLSelectElement
    expect(placement.value).toBe('prefer_extremes')
    expect(Array.from(placement.options).map(option => [option.value, option.textContent])).toEqual([['prefer_extremes', 'Fewest nodes, prefer extremes'], ['at_extremes', 'Always at extremes'], ['fewest', 'Fewest nodes only']])
    const snap = screen.getByLabelText(/Snap straight lines/) as HTMLSelectElement
    expect(Array.from(snap.options).map(option => [option.value, option.textContent])).toEqual([['off', 'Off'], ['axis', 'Horizontal & vertical'], ['axis_diagonal', 'Also 45°']])
    expect((screen.getByLabelText(/Nodes at tightest bends/) as HTMLInputElement).checked).toBe(true)
    const angle = screen.getByLabelText(/Snap angle/) as HTMLInputElement
    expect([angle.value, angle.min, angle.max]).toEqual(['2', '0.5', '10'])
    const scale = screen.getByLabelText(/Curve feature scale \(‰\)/) as HTMLInputElement
    expect([scale.value, scale.min, scale.max]).toEqual(['2', '0.25', '16'])
    expect(document.querySelectorAll('.field-dirty')).toHaveLength(0)
  })

  it('edits each option under its own key and marks it dirty', () => {
    const setTrace = vi.fn()
    renderTrace(DEFAULT_TRACE_SETTINGS, setTrace)
    fireEvent.change(screen.getByLabelText(/^Node placement/), { target: { value: 'at_extremes' } })
    expect(setTrace).toHaveBeenLastCalledWith({ ...DEFAULT_TRACE_SETTINGS, node_placement: 'at_extremes' })
    fireEvent.click(screen.getByLabelText(/Nodes at tightest bends/))
    expect(setTrace).toHaveBeenLastCalledWith({ ...DEFAULT_TRACE_SETTINGS, nodes_at_bends: false })
    fireEvent.change(screen.getByLabelText(/Curve feature scale/), { target: { value: '8' } })
    expect(setTrace).toHaveBeenLastCalledWith({ ...DEFAULT_TRACE_SETTINGS, curve_feature_scale: 8 })
    fireEvent.change(screen.getByLabelText(/Snap straight lines/), { target: { value: 'axis_diagonal' } })
    expect(setTrace).toHaveBeenLastCalledWith({ ...DEFAULT_TRACE_SETTINGS, straight_line_snap: 'axis_diagonal' })
    fireEvent.change(screen.getByLabelText(/Snap angle/), { target: { value: '5' } })
    expect(setTrace).toHaveBeenLastCalledWith({ ...DEFAULT_TRACE_SETTINGS, snap_angle: 5 })
    cleanup()
    renderTrace({ ...DEFAULT_TRACE_SETTINGS, node_placement: 'fewest', nodes_at_bends: false, straight_line_snap: 'off' }, () => undefined)
    for (const label of [/^Node placement/, /^Snap straight lines/]) expect(screen.getByLabelText(label).closest('label')?.querySelector('.field-dirty')).toBeTruthy()
    expect(screen.getByLabelText(/Nodes at tightest bends/).closest('label')?.querySelector('.field-dirty')).toBeTruthy()
    expect(screen.getByLabelText(/Curve feature scale/).closest('label')?.querySelector('.field-dirty')).toBeNull()
  })

  it('disables the snap angle while snapping is off', () => {
    renderTrace({ ...DEFAULT_TRACE_SETTINGS, straight_line_snap: 'off' }, () => undefined)
    expect((screen.getByLabelText(/Snap angle/) as HTMLInputElement).disabled).toBe(true)
  })

  it('loads stored settings without the new fields with their defaults', () => {
    const { node_placement: _a, nodes_at_bends: _b, curve_feature_scale: _c, straight_line_snap: _d, snap_angle: _e, ...stored } = DEFAULT_TRACE_SETTINGS
    expect({ ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings({ ...stored, curve_fit_tolerance: 1 }) }).toEqual({ ...DEFAULT_TRACE_SETTINGS, curve_fit_tolerance: 1 })
  })

  it('says the corner threshold is only the corner detector\'s and the tolerance sets the node count', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    const corner = screen.getByLabelText(/Corner threshold/).closest('label')?.querySelector('small')?.textContent ?? ''
    expect(corner).toMatch(/where two smooth sides meet \(straight or curved\) to become a hard corner/)
    expect(corner).toMatch(/placed by Node placement/)
    const tolerance = screen.getByLabelText(/Curve-fit tolerance/).closest('label')?.querySelector('small')?.textContent ?? ''
    expect(tolerance).toMatch(/sets the node count directly/)
  })
})

describe('trace lengths in thousandths of the image', () => {
  const lengthLabels: [string, TraceLengthField][] = [['Curve-fit tolerance', 'curve_fit_tolerance'], ['Straight-line deviation', 'straight_line_max_deviation'], ['Minimum straight length', 'minimum_straight_length'], ['Maximum deviation', 'max_deviation'], ['Side smoothness', 'side_smoothness'], ['Corner sharpening allowance', 'corner_sharpening_allowance'], ['Analysis scale', 'corner_analysis_span'], ['Curve feature scale', 'curve_feature_scale']]
  const help = (label: string) => screen.getByLabelText(new RegExp(`^${label}`)).closest('label')?.querySelector('small')?.textContent ?? ''

  it('says what each length comes to in pixels on the traced image', () => {
    render(<PipelineControls stage="trace" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={{ ...DEFAULT_TRACE_SETTINGS, curve_fit_tolerance: 1.5 }} setTrace={() => undefined} baseTrace={DEFAULT_TRACE_SETTINGS} paletteColours={[]}
      selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} tracedSize={{ width: 4000, height: 6000 }} />)
    // The longer side (6000 px) sets the scale: 1.5 thousandths are 9 px.
    expect(help('Curve-fit tolerance')).toMatch(/= 9 px on this image\.$/)
    expect(help('Straight-line deviation')).toMatch(/= 4\.8 px on this image\.$/)
    expect(help('Analysis scale')).toMatch(/= 24 px on this image\.$/)
    // An automatic maximum deviation shows its automatic value.
    expect(help('Maximum deviation')).toMatch(/= 6 px on this image \(Automatic\)\.$/)
    expect(help('Side smoothness')).toMatch(/= 4\.8 px on this image\.$/)
    // An automatic corner sharpening allowance follows the (also automatic) maximum deviation.
    expect(help('Corner sharpening allowance')).toMatch(/= 6 px on this image \(Automatic\)\.$/)
    for (const [label] of lengthLabels) expect(help(label)).toMatch(/px on this image/)
  })

  it('shows no pixel hint before the traced image is measured', () => {
    renderTrace(DEFAULT_TRACE_SETTINGS, () => undefined)
    for (const [label] of lengthLabels) expect(help(label)).not.toMatch(/px on this image/)
  })

  it('formats hints by size', () => {
    expect(pixelHint('curve_fit_tolerance', .75, 600)).toBe(' = 1 px on this image, held at the 1 px minimum.')
    expect(pixelHint('max_deviation', null, 600)).toBe(' = 1 px on this image (Automatic), held at the 1 px minimum.')
    expect(pixelHint('straight_line_max_deviation', .8, 600)).toBe(' = 0.48 px on this image.')
    expect(pixelHint('minimum_straight_length', 2, 500)).toBe(' = just over 1 px on this image, held above one pixel step.')
    expect(pixelHint('minimum_straight_length', 2, 256)).toBe(' = just over 1 px on this image, held above one pixel step.')
    expect(pixelHint('minimum_straight_length', 2, 600)).toBe(' = 1.2 px on this image.')
    expect(pixelHint('curve_fit_tolerance', .75, 2000)).toBe(' = 1.5 px on this image.')
    expect(pixelHint('corner_analysis_span', 4, 11264)).toBe(' = 45 px on this image.')
    expect(pixelHint('curve_fit_tolerance', .75, null)).toBe('')
    // Corner sharpening allowance has no automatic value of its own: without an explicit
    // fallback (the caller's job, since it follows the maximum deviation) an automatic
    // (null) allowance hints nothing.
    expect(pixelHint('corner_sharpening_allowance', null, 6000)).toBe('')
    expect(pixelHint('corner_sharpening_allowance', null, 6000, 1)).toBe(' = 6 px on this image (Automatic).')
    expect(pixelHint('corner_sharpening_allowance', 2, 6000, 1)).toBe(' = 12 px on this image.')
  })

  it('converts a pixel-era record on its image so it resolves back to its pixels', () => {
    expect(isPixelRecord({ curve_fit_tolerance: 1.5 })).toBe(true)
    expect(isPixelRecord({ length_unit: 'px' })).toBe(true)
    expect(isPixelRecord({ length_unit: 'per_mille', curve_fit_tolerance: 1 })).toBe(false)
    // A draft holding no length (only a layer order) has nothing to convert.
    expect(isPixelRecord({ layer_order: ['#000000'] })).toBe(false)
    const stored = { curve_fit_tolerance: 1.2, straight_line_max_deviation: 1.3, max_deviation: null, overlap: 2, curve_optimization: false }
    const converted = pixelRecordInUnits(stored, 1536) as Record<string, unknown>
    expect(converted.length_unit).toBe('per_mille')
    expect(converted.overlap).toBe(2)
    expect(converted.curve_optimization).toBe(false)
    const px = (name: TraceLengthField) => lengthInPixels(converted[name] as number, 1536)
    // Recorded lengths, the pixel-era defaults for absent ones, 2 px for an automatic
    // deviation, the recorded deviation for side smoothness (still carried as the old
    // `corner_straightness` key nowhere here, so it follows straight_line_max_deviation),
    // and the same automatic 2 px the corner sharpening allowance follows from the
    // maximum deviation.
    const expected: Record<TraceLengthField, number> = { curve_fit_tolerance: 1.2, straight_line_max_deviation: 1.3, minimum_straight_length: 4, max_deviation: 2, side_smoothness: 1.3, corner_sharpening_allowance: 2, corner_analysis_span: 8, curve_feature_scale: 4 }
    for (const name of TRACE_LENGTH_FIELDS) expect(px(name)).toBeCloseTo(expected[name], 9)
    expect(pixelRecordInUnits(converted, 1536)).toBe(converted)
  })

  it('converts a pixel-era record naming an explicit corner sharpening allowance', () => {
    const stored = { curve_fit_tolerance: 1.2, straight_line_max_deviation: 1.3, max_deviation: 3, corner_sharpening_allowance: 5 }
    const converted = pixelRecordInUnits(stored, 1536) as Record<string, unknown>
    expect(lengthInPixels(converted.corner_sharpening_allowance as number, 1536)).toBeCloseTo(5, 9)
  })
})

