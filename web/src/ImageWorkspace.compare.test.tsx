// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CANVAS_COMPARE_STORAGE_KEY, ImageWorkspace, NARROW_CANVAS_WIDTH, type WorkspaceLayer } from './ImageWorkspace'
import { splitPositionInImage } from './workspace'
import { CLEANUP_CHANGES_OVERLAY_KIND } from './CleanupPreview'
import { DEFAULT_CLEANUP_SETTINGS } from './PipelineControls'
import { PREVIEW_DEBOUNCE_MS } from './TracePreview'

const layers: WorkspaceLayer[] = [
  { id: 'source', name: 'Source', url: '/source.png', mediaType: 'image/png', role: 'image' },
  { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
  { id: 'report', name: 'Report', url: '/report.png', mediaType: 'image/png', role: 'image' },
]

beforeEach(() => {
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  localStorage.clear()
})

// Every observed element reports this width (and a fixed height) as soon as it is observed.
function stubCanvasWidth(width: number) {
  vi.stubGlobal('ResizeObserver', class {
    constructor(private readonly callback: ResizeObserverCallback) {}
    observe(target: Element) { this.callback([{ target, contentRect: { width, height: 600 } } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver) }
    unobserve() {}
    disconnect() {}
  })
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  localStorage.clear()
})

function load(container: HTMLElement, name: string, width: number, height: number) {
  for (const image of [...container.querySelectorAll('img')].filter(node => node.src.endsWith(`/${name}.png`)) as HTMLImageElement[]) {
    Object.defineProperties(image, { naturalWidth: { configurable: true, value: width }, naturalHeight: { configurable: true, value: height } })
    fireEvent.load(image)
  }
}

type Props = Partial<Parameters<typeof ImageWorkspace>[0]>
const canvas = (props: Props) => <ImageWorkspace layers={layers} editorMode canvasLabel="Flatten canvas" preferredLayerId="flat" {...props} />

// The Flatten canvas showing the flattened raster, with every image measured: source and
// flattened on one grid, the report on another.
function prepare(props: Props = {}, sizes: Record<string, [number, number]> = {}) {
  const result = render(canvas(props))
  const viewport = within(result.container).getByLabelText('Image comparison viewport')
  Object.defineProperties(viewport, { clientWidth: { configurable: true, value: 800 }, clientHeight: { configurable: true, value: 600 } })
  viewport.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) })
  HTMLElement.prototype.setPointerCapture = () => undefined
  const measured = { source: [1000, 700], flat: [1000, 700], report: [1000, 850], ...sizes }
  for (const [name, [width, height]] of Object.entries(measured)) load(result.container, name, width, height)
  return { ...result, viewport }
}

const compareGroup = () => screen.queryByRole('group', { name: 'Compare' })
const modeButton = (name: string) => within(compareGroup()!).getByRole('button', { name })
const stage = (container: HTMLElement) => container.querySelector('.image-stage') as HTMLElement

describe('main canvas comparison', () => {
  it('offers the Compare control, Off by default, where a Before image registers with the shown one', () => {
    const { container } = prepare({ compareBefore: ['source'] })
    expect(compareGroup()).toBeTruthy()
    expect(modeButton('Off').getAttribute('aria-pressed')).toBe('true')
    expect(modeButton('split').getAttribute('aria-pressed')).toBe('false')
    expect(container.querySelector('.compare-layer')).toBeNull()
    expect(container.querySelector('.split-divider')).toBeNull()
  })

  it('hides the Compare control where the canvas has no second image', () => {
    prepare()
    expect(compareGroup()).toBeNull()
    cleanup()
    // The only candidate is the image already shown.
    prepare({ compareBefore: ['flat'] })
    expect(compareGroup()).toBeNull()
    cleanup()
    // The candidate's grid does not register with the shown one.
    prepare({ compareBefore: ['report'] })
    expect(compareGroup()).toBeNull()
    cleanup()
    // The candidate is not among the canvas's layers.
    prepare({ compareBefore: ['missing'] })
    expect(compareGroup()).toBeNull()
  })

  it('registers the pre-upscale source through the recorded upscale, and nothing by tolerance', () => {
    // The tiled upscaler rounds each side to a multiple of 8: 1496 x 1051 at 2x is 2992 x 2104.
    const lineage = { id: 'source', upscaledId: 'report', before: { width: 1496, height: 1051 }, after: { width: 2992, height: 2104 } }
    for (const flatten of [1, 2, 4]) {
      const { container } = prepare({ compareBefore: ['source'], compareLineage: lineage }, { source: [1496, 1051], flat: [2992 * flatten, 2104 * flatten] })
      fireEvent.click(modeButton('split'))
      expect(within(container.querySelector('.compare-layer') as HTMLElement).getByAltText('Source')).toBeTruthy()
      fireEvent.click(modeButton('Off'))
      cleanup()
    }
    // Without the recorded upscale the same pair does not register.
    prepare({ compareBefore: ['source'] }, { source: [1496, 1051], flat: [2992, 2104] })
    expect(compareGroup()).toBeNull()
    cleanup()
    // Nor does a shown grid off the recorded one's whole multiples.
    prepare({ compareBefore: ['source'], compareLineage: lineage }, { source: [1496, 1051], flat: [2992, 2112] })
    expect(compareGroup()).toBeNull()
  })

  it('falls back to the two images\' own sizes when the upscale recorded none', () => {
    // 'report' stands for the upscaled source; the flattened raster is on its grid at 2x.
    const lineage = { id: 'source', upscaledId: 'report' }
    const { container } = prepare({ compareBefore: ['source'], compareLineage: lineage }, { source: [1496, 1051], report: [2992, 2104], flat: [5984, 4208] })
    fireEvent.click(modeButton('split'))
    expect(within(container.querySelector('.compare-layer') as HTMLElement).getByAltText('Source')).toBeTruthy()
    cleanup()
    prepare({ compareBefore: ['source'], compareLineage: lineage }, { source: [1496, 1051], report: [2992, 2104], flat: [5984, 4216] })
    expect(compareGroup()).toBeNull()
  })

  // The canvas's own width decides, not the window's: a narrow window still has an
  // inline control in a wide canvas.
  it('keeps the control inline in a canvas at least NARROW_CANVAS_WIDTH wide, whatever the window', () => {
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
    stubCanvasWidth(NARROW_CANVAS_WIDTH)
    const { container } = prepare({ compareBefore: ['source'] })
    expect(compareGroup()).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Compare: / })).toBeNull()
    expect(container.querySelector('.image-workspace')?.classList.contains('narrow-canvas')).toBe(false)
  })

  describe(`in a canvas narrower than NARROW_CANVAS_WIDTH`, () => {
    beforeEach(() => stubCanvasWidth(NARROW_CANVAS_WIDTH - 1))
    const toggle = () => screen.getByRole('button', { name: /^Compare: / })

    it('collapses into a toolbar button opening the control as a popover outside the toolbar row', () => {
      const { container } = prepare({ compareBefore: ['source'] })
      expect(toggle().getAttribute('aria-label')).toBe('Compare: Off')
      // The visible text can drop to the mode word; the full name stays the accessible one.
      expect(toggle().querySelector('.canvas-compare-toggle-prefix')?.textContent).toBe('Compare: ')
      expect(toggle().getAttribute('aria-expanded')).toBe('false')
      expect(compareGroup()).toBeNull()
      fireEvent.click(toggle())
      expect(toggle().getAttribute('aria-expanded')).toBe('true')
      const panel = compareGroup()!
      expect(toggle().getAttribute('aria-controls')).toBe(panel.id)
      expect(panel.closest('.viewport-toolbar')).toBeNull()
      expect(panel.parentElement).toBe(container.querySelector('.image-workspace'))
      fireEvent.click(modeButton('split'))
      expect(toggle().getAttribute('aria-label')).toBe('Compare: Split')
      // Escape closes it and returns focus to the button.
      fireEvent.keyDown(modeButton('split'), { key: 'Escape' })
      expect(toggle().getAttribute('aria-expanded')).toBe('false')
      expect(document.activeElement).toBe(toggle())
    })

    it('closes on a press on its backdrop, which reaches nothing beneath, with no document listeners', () => {
      const added = vi.spyOn(document, 'addEventListener')
      const select = vi.fn()
      const { container, viewport, unmount } = prepare({ compareBefore: ['source'], onSelectPoint: select })
      const zoom = vi.fn()
      screen.getByRole('button', { name: 'Zoom in' }).addEventListener('click', zoom)
      fireEvent.click(toggle())
      const backdrop = container.querySelector('.canvas-compare-backdrop') as HTMLElement
      // A full-page layer between the popover and everything else.
      expect(backdrop.parentElement).toBe(compareGroup()!.parentElement)
      expect(backdrop.nextElementSibling).toBe(compareGroup())
      // The whole press lands on the backdrop, as a browser delivers it: down, up, click.
      const events: string[] = []
      for (const type of ['pointerdown', 'pointerup', 'click']) backdrop.addEventListener(type, event => events.push(`${event.type}:${event.target === backdrop}`))
      fireEvent.pointerDown(backdrop, { pointerId: 1 })
      fireEvent.pointerUp(backdrop, { pointerId: 1 })
      expect(toggle().getAttribute('aria-expanded')).toBe('true')
      fireEvent.click(backdrop)
      expect(events).toEqual(['pointerdown:true', 'pointerup:true', 'click:true'])
      expect(toggle().getAttribute('aria-expanded')).toBe('false')
      expect(container.querySelector('.canvas-compare-backdrop')).toBeNull()
      expect(zoom).not.toHaveBeenCalled()
      expect(select).not.toHaveBeenCalled()
      // Closed, the canvas and its buttons take presses directly again.
      fireEvent.pointerDown(viewport, { pointerId: 2, clientX: 400, clientY: 300 })
      fireEvent.pointerUp(viewport, { pointerId: 2, clientX: 400, clientY: 300 })
      expect(select).toHaveBeenCalledTimes(1)
      fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
      expect(zoom).toHaveBeenCalledTimes(1)
      fireEvent.click(toggle())
      unmount()
      // Neither opening, closing nor unmounting ever listened on the document for presses.
      expect(added.mock.calls.filter(([type]) => ['pointerdown', 'pointerup', 'click'].includes(type))).toEqual([])
    })

    it('closes when focus leaves both the button and the popover', () => {
      prepare({ compareBefore: ['source'] })
      fireEvent.click(toggle())
      // Moving between the button and the popover keeps it open.
      fireEvent.blur(toggle(), { relatedTarget: modeButton('split') })
      expect(toggle().getAttribute('aria-expanded')).toBe('true')
      fireEvent.blur(modeButton('split'), { relatedTarget: toggle() })
      expect(toggle().getAttribute('aria-expanded')).toBe('true')
      // Tabbing on past it closes it.
      fireEvent.blur(modeButton('blink'), { relatedTarget: screen.getByRole('button', { name: 'Zoom in' }) })
      expect(toggle().getAttribute('aria-expanded')).toBe('false')
    })
  })

  it('opens Off, without saving, on a canvas that does not remember the mode', () => {
    localStorage.setItem(CANVAS_COMPARE_STORAGE_KEY, 'split')
    const { container, rerender } = prepare({ compareBefore: ['source'], compareRemembered: false, compareLocksEditing: true, compareKey: 'palette' })
    expect(modeButton('Off').getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('.compare-layer')).toBeNull()
    fireEvent.click(modeButton('blink'))
    // Comparing locks the stage's editing, so the control says so even without canvas tools.
    expect(within(compareGroup()!).getByText('Turn Compare off to edit')).toBeTruthy()
    expect(localStorage.getItem(CANVAS_COMPARE_STORAGE_KEY)).toBe('split')
    // Another such stage (Edit after Palette, same canvas label) opens Off again.
    rerender(canvas({ compareBefore: ['source'], compareRemembered: false, compareLocksEditing: true, compareKey: 'edit' }))
    expect(modeButton('Off').getAttribute('aria-pressed')).toBe('true')
    // A remembering canvas uses the saved mode.
    rerender(canvas({ compareBefore: ['source'] }))
    expect(modeButton('split').getAttribute('aria-pressed')).toBe('true')
  })

  it('draws the compared layer crisp while its own magnification is at least 1', () => {
    // Fitted at scale 0.768, the shown 1000-px image is reduced; a 500-px Before stretched
    // over it is enlarged 1.536x.
    const { container } = prepare({ compareBefore: ['source'] }, { source: [500, 350] })
    fireEvent.click(modeButton('split'))
    expect(stage(container).classList.contains('magnified')).toBe(false)
    expect((container.querySelector('.compare-layer img') as HTMLElement).classList.contains('magnified')).toBe(true)
    cleanup()
    const same = prepare({ compareBefore: ['source'] })
    fireEvent.click(modeButton('split'))
    expect((same.container.querySelector('.compare-layer img') as HTMLElement).classList.contains('magnified')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(stage(same.container).classList.contains('magnified')).toBe(true)
    expect((same.container.querySelector('.compare-layer img') as HTMLElement).classList.contains('magnified')).toBe(true)
  })

  it('reports whether it compares', () => {
    const changed = vi.fn()
    prepare({ compareBefore: ['source'], onComparingChange: changed })
    expect(changed).toHaveBeenLastCalledWith(false)
    fireEvent.click(modeButton('blink'))
    expect(changed).toHaveBeenLastCalledWith(true)
    fireEvent.click(modeButton('Off'))
    expect(changed).toHaveBeenLastCalledWith(false)
  })

  it('reports it stops comparing when the Before disappears and when it unmounts', () => {
    const changed = vi.fn()
    const { rerender, unmount } = prepare({ compareBefore: ['source'], onComparingChange: changed })
    fireEvent.click(modeButton('split'))
    expect(changed).toHaveBeenLastCalledWith(true)
    rerender(canvas({ compareBefore: [], onComparingChange: changed }))
    expect(changed).toHaveBeenLastCalledWith(false)
    rerender(canvas({ compareBefore: ['source'], onComparingChange: changed }))
    expect(changed).toHaveBeenLastCalledWith(true)
    // Leaving for a stage without a canvas (Create, Export) unmounts it.
    unmount()
    expect(changed).toHaveBeenLastCalledWith(false)
  })

  it('splits the shown image (left of the divider) against the Before (right), as the inspector does, draggable by pointer and keys', () => {
    const { container } = prepare({ compareBefore: ['source'] })
    fireEvent.click(modeButton('split'))
    expect(modeButton('split').getAttribute('aria-pressed')).toBe('true')
    const compare = container.querySelector('.compare-layer') as HTMLElement
    // The stage keeps the shown (After) image; the Before is the clipped layer over it.
    expect(stage(container).querySelector(':scope > img')?.getAttribute('alt')).toBe('Flattened')
    expect(within(compare).getByAltText('Source')).toBeTruthy()
    expect(within(compareGroup()!).getByText('Before: Source')).toBeTruthy()
    const handle = screen.getByRole('slider', { name: 'Comparison split position' })
    const clip = compare.style.clipPath
    fireEvent.keyDown(handle, { key: 'ArrowRight' })
    expect(handle.getAttribute('aria-valuenow')).toBe('51')
    fireEvent.keyDown(handle, { key: 'Home' })
    expect(handle.getAttribute('aria-valuenow')).toBe('0')
    const panned = stage(container).style.transform
    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 0 })
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 200 })
    fireEvent.pointerUp(handle, { pointerId: 1 })
    expect(handle.getAttribute('aria-valuenow')).toBe('25')
    expect(compare.style.clipPath).not.toBe(clip)
    // Dragging the divider never pans the artwork.
    expect(stage(container).style.transform).toBe(panned)
  })

  it('keeps the split registered with the shared zoom and pan', () => {
    const { container } = prepare({ compareBefore: ['source'] })
    fireEvent.click(modeButton('split'))
    const compare = container.querySelector('.compare-layer') as HTMLElement
    // The Before layer lives inside the transformed stage, so it zooms and pans with the shown image.
    expect(compare.parentElement).toBe(stage(container))
    const insetFor = () => {
      const [, x, y, scale] = stage(container).style.transform.match(/translate3d\(([-\d.]+)px, ([-\d.]+)px, 0\) scale\(([-\d.]+)\)/)!.map(Number)
      return `inset(0 0 0 ${splitPositionInImage(800, 50, 1000, { x, y, scale })}px)`
    }
    const fitted = stage(container).style.transform
    expect(compare.style.clipPath).toBe(insetFor())
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(stage(container).style.transform).not.toBe(fitted)
    // The divider stays at the same screen position, so it cuts the zoomed image elsewhere.
    expect(compare.style.clipPath).toBe(insetFor())
  })

  it('blinks between the shown image and the Before, with pause and manual stepping', () => {
    vi.useFakeTimers()
    const { container } = prepare({ compareBefore: ['source'] })
    fireEvent.click(modeButton('blink'))
    expect(container.querySelector('.compare-layer')).toBeNull()
    act(() => { vi.advanceTimersByTime(550) })
    expect(within(container.querySelector('.compare-layer') as HTMLElement).getByAltText('Source')).toBeTruthy()
    act(() => { vi.advanceTimersByTime(550) })
    expect(container.querySelector('.compare-layer')).toBeNull()
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'Pause blink' }))
    act(() => { vi.advanceTimersByTime(2000) })
    expect(container.querySelector('.compare-layer')).toBeNull()
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'Show before' }))
    expect(within(container.querySelector('.compare-layer') as HTMLElement).getByAltText('Source')).toBeTruthy()
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'Show after' }))
    expect(container.querySelector('.compare-layer')).toBeNull()
    expect(within(compareGroup()!).getByRole('button', { name: 'Resume blink' })).toBeTruthy()
    // No divider in blink.
    expect(container.querySelector('.split-divider')).toBeNull()
  })

  it('offers a Before picker when several images can be compared', () => {
    const { container } = prepare({ compareBefore: ['source', 'report', 'other'], layers: [...layers, { id: 'other', name: 'Other', url: '/other.png', mediaType: 'image/png', role: 'image' }] }, { report: [1000, 700], other: [500, 350] } as Record<string, [number, number]>)
    fireEvent.click(modeButton('split'))
    const picker = within(compareGroup()!).getByLabelText('Before') as HTMLSelectElement
    expect([...picker.options].map(option => option.value)).toEqual(['source', 'report', 'other'])
    expect(picker.value).toBe('source')
    fireEvent.change(picker, { target: { value: 'report' } })
    expect(within(container.querySelector('.compare-layer') as HTMLElement).getByAltText('Report')).toBeTruthy()
  })

  it('remembers the chosen mode, and keeps it while a canvas has nothing to compare', () => {
    const first = prepare({ compareBefore: ['source'] })
    fireEvent.click(modeButton('split'))
    expect(localStorage.getItem(CANVAS_COMPARE_STORAGE_KEY)).toBe('split')
    // A canvas without a Before shows no comparison and no control.
    first.rerender(canvas({ compareBefore: [] }))
    expect(compareGroup()).toBeNull()
    expect(first.container.querySelector('.compare-layer')).toBeNull()
    expect(first.container.querySelector('.split-divider')).toBeNull()
    first.rerender(canvas({ compareBefore: ['source'] }))
    expect(modeButton('split').getAttribute('aria-pressed')).toBe('true')
    expect(first.container.querySelector('.compare-layer')).toBeTruthy()
    cleanup()
    // A fresh canvas starts in the remembered mode.
    const second = prepare({ compareBefore: ['source'] })
    expect(modeButton('split').getAttribute('aria-pressed')).toBe('true')
    expect(second.container.querySelector('.split-divider')).toBeTruthy()
    fireEvent.click(modeButton('Off'))
    expect(localStorage.getItem(CANVAS_COMPARE_STORAGE_KEY)).toBe('single')
    expect(second.container.querySelector('.compare-layer')).toBeNull()
  })

  it('hides inspection overlays and the recolour preview while comparing, keeping their switches', () => {
    const withOverlay: WorkspaceLayer[] = [...layers, { id: 'fallbacks', name: 'Fallbacks', url: '/fallbacks.png', mediaType: 'image/png', role: 'overlay', kind: 'fallbacks' }]
    const frame = { bounds: { x: 0, y: 0, width: 1, height: 1 }, data: new Uint8ClampedArray(4) }
    const { container } = prepare({ layers: withOverlay, inspectionKinds: ['fallbacks'], overlay: frame, compareBefore: ['source'] }, { fallbacks: [1000, 700] } as Record<string, [number, number]>)
    const toggle = within(screen.getByRole('group', { name: 'Inspection layers' })).getByLabelText('Fallbacks') as HTMLInputElement
    fireEvent.click(toggle)
    expect(screen.getByAltText('Fallbacks')).toBeTruthy()
    expect(container.querySelector('.editor-overlay')).toBeTruthy()
    expect(within(compareGroup()!).queryByText('Overlays and live preview hidden while comparing')).toBeNull()
    for (const mode of ['split', 'blink']) {
      fireEvent.click(modeButton(mode))
      expect(screen.queryByAltText('Fallbacks')).toBeNull()
      expect(container.querySelector('.editor-overlay')).toBeNull()
      expect(within(compareGroup()!).getByText('Overlays and live preview hidden while comparing')).toBeTruthy()
      // The switch keeps its state.
      expect(toggle.checked).toBe(true)
    }
    fireEvent.click(modeButton('Off'))
    expect(screen.getByAltText('Fallbacks')).toBeTruthy()
    expect(container.querySelector('.editor-overlay')).toBeTruthy()
  })

  it('disables area selection, preservation picking and the pencil while comparing', () => {
    const select = vi.fn()
    const { viewport } = prepare({ compareBefore: ['source'], onSelectPoint: select, pickAim: true })
    const click = () => {
      fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 400, clientY: 300 })
      fireEvent.pointerUp(viewport, { pointerId: 1, clientX: 400, clientY: 300 })
    }
    fireEvent.click(modeButton('split'))
    expect(within(compareGroup()!).getByText('Turn Compare off to edit')).toBeTruthy()
    expect(viewport.classList.contains('selecting-point')).toBe(false)
    click()
    expect(select).not.toHaveBeenCalled()
    fireEvent.click(modeButton('Off'))
    expect(within(compareGroup()!).queryByText('Turn Compare off to edit')).toBeNull()
    click()
    expect(select).toHaveBeenCalledTimes(1)
    cleanup()
    const paint = vi.fn()
    const painted = prepare({ compareBefore: ['source'], paintMode: true, onPaint: paint })
    fireEvent.click(modeButton('blink'))
    expect(within(compareGroup()!).getByText('Turn Compare off to edit')).toBeTruthy()
    fireEvent.pointerDown(painted.viewport, { pointerId: 1, clientX: 400, clientY: 300 })
    fireEvent.pointerMove(painted.viewport, { pointerId: 1, clientX: 420, clientY: 300 })
    fireEvent.pointerUp(painted.viewport, { pointerId: 1, clientX: 420, clientY: 300 })
    expect(paint).not.toHaveBeenCalled()
    expect(painted.viewport.classList.contains('painting')).toBe(false)
    fireEvent.click(modeButton('Off'))
    fireEvent.pointerDown(painted.viewport, { pointerId: 2, clientX: 400, clientY: 300 })
    expect(paint).toHaveBeenCalled()
  })

  it('hides the Cleanup changes overlay and the live preview while comparing', async () => {
    vi.useFakeTimers()
    const pending: { resolve: (response: unknown) => void; signal?: AbortSignal | null }[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => new Promise(resolve => { if (!url.endsWith('/cleanup-preview/regions')) pending.push({ resolve, signal: init.signal }) })))
    const withChanges: WorkspaceLayer[] = [...layers, { id: 'changes', name: 'Cleanup changes', url: '/changes.png', mediaType: 'image/png', role: 'overlay', kind: CLEANUP_CHANGES_OVERLAY_KIND }]
    const { container } = prepare({
      layers: withChanges, canvasLabel: 'Cleanup canvas', inspectionKinds: [CLEANUP_CHANGES_OVERLAY_KIND], compareBefore: ['source'],
      cleanupPreview: { revisionId: 'rev-1', settings: { ...DEFAULT_CLEANUP_SETTINGS, width_mm: 300 }, tracedSize: { width: 1000, height: 700 } },
    }, { changes: [1000, 700] } as Record<string, [number, number]>)
    // Show changes is on by default.
    expect(screen.getByAltText('Cleanup changes')).toBeTruthy()
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    expect(pending).toHaveLength(1)
    const window = { x: 166, y: 100, width: 668, height: 500 }
    await act(async () => {
      pending[0].resolve({ ok: true, statusText: 'OK', json: async () => ({ window, crop: window, image: { width: 1000, height: 700 }, pixels: 'data:image/png;base64,cA==', changes: 'data:image/png;base64,Yw==', removed_pixels: 1, widened_pixels: 0, elapsed_ms: 1 }) })
    })
    expect(container.querySelector('.cleanup-preview-pixels')).toBeTruthy()
    expect(screen.getByText(/^Live: /)).toBeTruthy()
    fireEvent.click(modeButton('split'))
    expect(screen.queryByText(/^Live: /)).toBeNull()
    expect(within(container).getByLabelText('Image comparison viewport').classList.contains('comparing')).toBe(true)
    expect(screen.queryByAltText('Cleanup changes')).toBeNull()
    expect(container.querySelector('.cleanup-preview-pixels')).toBeNull()
    expect(container.querySelector('.cleanup-preview-changes')).toBeNull()
    expect(within(compareGroup()!).getByText('Overlays and live preview hidden while comparing')).toBeTruthy()
    expect((screen.getByRole('switch', { name: 'Show changes' }) as HTMLInputElement).checked).toBe(true)
    expect((screen.getByRole('switch', { name: 'Live preview' }) as HTMLInputElement).checked).toBe(true)
    // Held while comparing: a pan sends no request.
    const viewport = within(container).getByLabelText('Image comparison viewport')
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 400, clientY: 300 })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 380, clientY: 300 })
    fireEvent.pointerUp(viewport, { pointerId: 1, clientX: 380, clientY: 300 })
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS * 4) })
    expect(pending).toHaveLength(1)
    fireEvent.click(modeButton('Off'))
    expect(screen.getByAltText('Cleanup changes')).toBeTruthy()
    // It resumes on Off, previewing the panned view.
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    expect(pending).toHaveLength(2)
  })

  it('aborts a live preview request in flight when comparing starts', () => {
    vi.useFakeTimers()
    const pending: { signal?: AbortSignal | null }[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => new Promise(() => { if (!url.endsWith('/cleanup-preview/regions')) pending.push({ signal: init.signal }) })))
    prepare({ canvasLabel: 'Cleanup canvas', compareBefore: ['source'],
      cleanupPreview: { revisionId: 'rev-1', settings: { ...DEFAULT_CLEANUP_SETTINGS, width_mm: 300 }, tracedSize: { width: 1000, height: 700 } } })
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    expect(pending).toHaveLength(1)
    expect(pending[0].signal?.aborted).toBe(false)
    fireEvent.click(modeButton('blink'))
    expect(pending[0].signal?.aborted).toBe(true)
    expect(screen.queryByText('Updating live preview…')).toBeNull()
  })

  it('reports a failed image load in the Compare control, with a Retry', () => {
    const retry = vi.fn()
    prepare({ compareStatus: { message: 'Could not load the source from before the upscale: boom', onRetry: retry } })
    const group = compareGroup()!
    expect(within(group).getByRole('alert').textContent).toContain('Could not load the source from before the upscale: boom')
    // Nothing to compare yet, so no mode buttons.
    expect(within(group).queryByRole('button', { name: 'split' })).toBeNull()
    fireEvent.click(within(group).getByRole('button', { name: 'Retry' }))
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it('leaves the inspector without the canvas Compare control', () => {
    render(<ImageWorkspace layers={layers} compareBefore={['source']} />)
    expect(compareGroup()).toBeNull()
  })
})
