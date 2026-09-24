// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ImageWorkspace, type CanvasMarker, type WorkspaceLayer } from './ImageWorkspace'
import { DEFAULT_TRACE_SETTINGS } from './PipelineControls'
import { LIVE_PREVIEW_STORAGE_KEY, PREVIEW_DEBOUNCE_MS } from './TracePreview'

const TRACE_KINDS = ['trace-missed-details-overlay', 'trace-fallbacks-overlay']
const layers: WorkspaceLayer[] = [
  { id: 'source', name: 'Source', url: '/source.png', mediaType: 'image/png', role: 'image' },
  { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
  { id: 'report', name: 'Report', url: '/report.png', mediaType: 'image/png', role: 'image' },
  { id: 'mask', name: 'Edit mask', url: '/mask.png', mediaType: 'image/png', role: 'mask' },
]

beforeEach(() => {
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function load(container: HTMLElement, name: string, width: number, height: number) {
  const image = [...container.querySelectorAll('img')].find(node => node.src.endsWith(`/${name}.png`)) as HTMLImageElement
  Object.defineProperties(image, { naturalWidth: { configurable: true, value: width }, naturalHeight: { configurable: true, value: height } })
  fireEvent.load(image)
}

function prepare(props: Partial<Parameters<typeof ImageWorkspace>[0]> = {}) {
  const result = render(<ImageWorkspace layers={layers} {...props} />)
  const viewport = within(result.container).getByLabelText(props.selectionLabel ?? 'Image comparison viewport')
  Object.defineProperties(viewport, {
    clientWidth: { configurable: true, value: 800 },
    clientHeight: { configurable: true, value: 600 },
  })
  viewport.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) })
  HTMLElement.prototype.setPointerCapture = () => undefined
  load(result.container, 'source', 1000, 700)
  load(result.container, 'flat', 1000, 700)
  load(result.container, 'report', 1000, 850)
  load(result.container, 'mask', 1000, 700)
  return { ...result, viewport }
}

describe('ImageWorkspace interactions', () => {
  it('renders an enabled mask on the same shared image stage', () => {
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    fireEvent.click(within(desktop).getByLabelText('Edit mask'))
    const mask = screen.getByAltText('Edit mask')
    expect(mask.classList.contains('mask')).toBe(true)
    expect(mask.parentElement?.classList.contains('image-stage')).toBe(true)
  })

  it('blocks a misleading comparison when intrinsic dimensions differ', () => {
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    const after = within(desktop).getByLabelText('After') as HTMLSelectElement
    fireEvent.change(after, { target: { value: 'report' } })
    fireEvent.click(within(desktop).getByText('split'))
    expect(within(desktop).getByRole('status').textContent).toContain('different pixel dimensions')
    expect(screen.queryByAltText('Report')).toBeNull()
  })

  it('pans with one pointer and pinches around a shared transform', () => {
    const { container, viewport } = prepare()
    const stage = container.querySelector('.image-stage') as HTMLElement
    const initial = stage.style.transform
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 140, clientY: 125 })
    expect(stage.style.transform).not.toBe(initial)
    const panned = stage.style.transform
    fireEvent.pointerDown(viewport, { pointerId: 2, clientX: 200, clientY: 100 })
    fireEvent.pointerMove(viewport, { pointerId: 2, clientX: 260, clientY: 100 })
    expect(stage.style.transform).not.toBe(panned)
    fireEvent.pointerUp(viewport, { pointerId: 2 })
    fireEvent.pointerUp(viewport, { pointerId: 1 })
  })

  it('paints a continuous pixel stroke on drag instead of panning in paint mode', () => {
    const onPaint = vi.fn()
    // Image 1000x700 in an 800x600 viewport fits at scale 0.768, origin (16, 31.2),
    // so image pixel (px, py) sits at client (16 + px*0.768, 31.2 + py*0.768).
    const { container, viewport } = prepare({ paintMode: true, onPaint, selectionLabel: 'Flattened artwork pencil' })
    const stage = container.querySelector('.image-stage') as HTMLElement
    const before = stage.style.transform
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 16 + 100 * 0.768, clientY: 31.2 + 100 * 0.768 })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 16 + 103 * 0.768, clientY: 31.2 + 100 * 0.768 })
    fireEvent.pointerUp(viewport, { pointerId: 1 })
    // The drag did not move the artwork...
    expect(stage.style.transform).toBe(before)
    // ...it painted the starting pixel and every pixel across to the destination.
    const painted = onPaint.mock.calls.map(call => `${call[0]},${call[1]}`)
    expect(painted).toContain('100,100')
    expect(painted).toContain('101,100')
    expect(painted).toContain('102,100')
    expect(painted).toContain('103,100')
  })

  it('exposes phone controls with touch-sized primary actions', () => {
    const { container } = prepare()
    const toolbarButton = screen.getByRole('button', { name: 'Controls' })
    fireEvent.click(toolbarButton)
    const sheet = container.querySelector('.bottom-sheet') as HTMLElement
    expect(sheet.classList.contains('open')).toBe(true)
    expect(within(sheet).getByRole('button', { name: 'Done' })).toBeTruthy()
    fireEvent.click(within(sheet).getByRole('button', { name: 'split' }))
    expect(within(sheet).getByLabelText('Split position', { exact: true })).toBeTruthy()
  })

  it('exposes comparison selection state and manual blink controls', () => {
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    const blink = within(desktop).getByRole('button', { name: 'blink' })
    expect(blink.getAttribute('aria-pressed')).toBe('false')
    fireEvent.change(within(desktop).getByLabelText('After'), { target: { value: 'flat' } })
    fireEvent.click(blink)
    expect(blink.getAttribute('aria-pressed')).toBe('true')
    expect(within(desktop).getByRole('button', { name: 'Pause blink' })).toBeTruthy()
    fireEvent.click(within(desktop).getByRole('button', { name: 'Show after image' }))
    expect(screen.getByAltText('Flattened')).toBeTruthy()
    expect(within(desktop).getByRole('button', { name: 'Show before image' })).toBeTruthy()
  })

  it('keeps split mode and the selected before image when the Before dropdown changes', () => {
    const result = prepare({ preferredLayerId: 'source' })
    const desktop = result.container.querySelector('.desktop-view-controls') as HTMLElement
    const before = within(desktop).getByLabelText('Before') as HTMLSelectElement

    fireEvent.click(within(desktop).getByRole('button', { name: 'split' }))
    fireEvent.change(before, { target: { value: 'flat' } })

    expect(before.value).toBe('flat')
    expect(within(desktop).getByRole('button', { name: 'split' }).getAttribute('aria-pressed')).toBe('true')
    expect(within(desktop).getByLabelText('Split position', { exact: true })).toBeTruthy()
  })

  it('starts blink paused when reduced motion is preferred', () => {
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    fireEvent.click(within(desktop).getByRole('button', { name: 'blink' }))
    expect(within(desktop).getByRole('button', { name: 'Resume blink' })).toBeTruthy()
    expect(within(desktop).getByText(/reduced-motion/).textContent).toContain('reduced-motion')
    fireEvent.click(within(desktop).getByRole('button', { name: 'Resume blink' }))
    expect(within(desktop).getByRole('button', { name: 'Pause blink' })).toBeTruthy()
    expect(within(desktop).queryByText(/reduced-motion/)).toBeNull()
  })

  it('treats phone controls as a modal, traps focus, closes on Escape, and restores focus', () => {
    const { container } = prepare()
    const trigger = screen.getByRole('button', { name: 'Controls' })
    trigger.focus()
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog', { name: 'Workspace controls' })
    const done = within(dialog).getByRole('button', { name: 'Done' })
    expect(document.activeElement).toBe(done)
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(container.querySelector('.viewport')?.hasAttribute('inert')).toBe(true)

    const focusable = [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled), select:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])')]
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(focusable[focusable.length - 1])
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('disables comparison modes and explains why when only one image exists', () => {
    const single: WorkspaceLayer[] = [{ id: 'source', name: 'Source', url: '/source.png', mediaType: 'image/png', role: 'image' }]
    const result = render(<ImageWorkspace layers={single} />)
    const viewport = within(result.container).getByLabelText('Image comparison viewport')
    Object.defineProperties(viewport, { clientWidth: { configurable: true, value: 800 }, clientHeight: { configurable: true, value: 600 } })
    load(result.container, 'source', 1000, 700)
    const desktop = result.container.querySelector('.desktop-view-controls') as HTMLElement
    const split = within(desktop).getByRole('button', { name: 'split' }) as HTMLButtonElement
    const blink = within(desktop).getByRole('button', { name: 'blink' }) as HTMLButtonElement
    expect(split.disabled).toBe(true)
    expect(split.getAttribute('aria-disabled')).toBe('true')
    expect(blink.disabled).toBe(true)
    expect(within(desktop).getByRole('status').textContent).toContain('matching pixel dimensions')
    // Before and After cannot resolve to two rendered copies of the same artifact.
    expect(result.container.querySelectorAll('.compare-layer').length).toBe(0)
    expect(result.container.querySelectorAll('.image-stage img').length).toBe(1)
  })

  it('renders a distinct After image when entering split with a compatible pair', () => {
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    const before = within(desktop).getByLabelText('Before') as HTMLSelectElement
    const after = within(desktop).getByLabelText('After') as HTMLSelectElement
    expect(before.value).toBe('source')
    expect(after.value).toBe('flat')
    expect(after.value).not.toBe(before.value)
    fireEvent.click(within(desktop).getByRole('button', { name: 'split' }))
    // A real After image is drawn, not a difference-free copy of Before.
    expect(screen.getByAltText('Flattened')).toBeTruthy()
    expect(container.querySelector('.compare-layer')).toBeTruthy()
    expect(container.querySelector('[role="slider"][aria-label="Comparison split position"]')).toBeTruthy()
  })

  it('drags the divider to move the split without panning the image', () => {
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    fireEvent.click(within(desktop).getByRole('button', { name: 'split' }))
    const stage = container.querySelector('.image-stage') as HTMLElement
    const compare = container.querySelector('.compare-layer') as HTMLElement
    const handle = screen.getByRole('slider', { name: 'Comparison split position' })
    const stageBefore = stage.style.transform
    const clipBefore = compare.style.clipPath
    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 400 })
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 200 })
    fireEvent.pointerUp(handle, { pointerId: 1 })
    // 200px of an 800px viewport is 25%.
    expect(handle.getAttribute('aria-valuenow')).toBe('25')
    expect(compare.style.clipPath).not.toBe(clipBefore)
    // Dragging the divider must never pan the artwork.
    expect(stage.style.transform).toBe(stageBefore)
  })

  it('reports a horizontal orientation for the split divider slider', () => {
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    fireEvent.click(within(desktop).getByRole('button', { name: 'split' }))
    const handle = screen.getByRole('slider', { name: 'Comparison split position' })
    // The value axis is horizontal (ArrowRight/clientX increase the split), so the
    // slider must advertise a horizontal orientation.
    expect(handle.getAttribute('aria-orientation')).toBe('horizontal')
  })

  it('announces an integer percentage after a fractional divider drag', () => {
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    fireEvent.click(within(desktop).getByRole('button', { name: 'split' }))
    const handle = screen.getByRole('slider', { name: 'Comparison split position' })
    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 400 })
    // 205px of an 800px viewport is 25.625%, which must be announced as a rounded integer.
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 205 })
    fireEvent.pointerUp(handle, { pointerId: 1 })
    expect(handle.getAttribute('aria-valuenow')).toBe('26')
    expect(handle.getAttribute('aria-valuetext')).toBe('26%')
  })

  it('keeps split mode while a newly selected Before image is still measuring', () => {
    const withExtra: WorkspaceLayer[] = [
      { id: 'source', name: 'Source', url: '/source.png', mediaType: 'image/png', role: 'image' },
      { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
      { id: 'other', name: 'Other', url: '/other.png', mediaType: 'image/png', role: 'image' },
    ]
    const result = render(<ImageWorkspace layers={withExtra} />)
    const viewport = within(result.container).getByLabelText('Image comparison viewport')
    Object.defineProperties(viewport, { clientWidth: { configurable: true, value: 800 }, clientHeight: { configurable: true, value: 600 } })
    // Measure source and flat (compatible); leave 'other' unmeasured.
    load(result.container, 'source', 1000, 700)
    load(result.container, 'flat', 1000, 700)
    const desktop = result.container.querySelector('.desktop-view-controls') as HTMLElement
    fireEvent.click(within(desktop).getByRole('button', { name: 'split' }))
    expect(within(desktop).getByRole('button', { name: 'split' }).getAttribute('aria-pressed')).toBe('true')
    // Switching Before to the unmeasured image makes availability unknown, not false,
    // so split mode must persist rather than being force-dropped to single (F3).
    fireEvent.change(within(desktop).getByLabelText('Before'), { target: { value: 'other' } })
    expect(within(desktop).getByRole('button', { name: 'split' }).getAttribute('aria-pressed')).toBe('true')
    // Once measured and compatible the comparison stays available.
    load(result.container, 'other', 1000, 700)
    expect(within(desktop).getByRole('button', { name: 'split' }).getAttribute('aria-pressed')).toBe('true')
    result.unmount()
  })

  it('moves the split with the keyboard on the divider handle', () => {
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    fireEvent.click(within(desktop).getByRole('button', { name: 'split' }))
    const handle = screen.getByRole('slider', { name: 'Comparison split position' })
    handle.focus()
    fireEvent.keyDown(handle, { key: 'ArrowRight' })
    expect(handle.getAttribute('aria-valuenow')).toBe('51')
    fireEvent.keyDown(handle, { key: 'Home' })
    expect(handle.getAttribute('aria-valuenow')).toBe('0')
    fireEvent.keyDown(handle, { key: 'End' })
    expect(handle.getAttribute('aria-valuenow')).toBe('100')
  })

  it('isolates the full application background and redirects programmatic outside focus', () => {
    const result = render(<main><button aria-hidden="false">Outside action</button><section><ImageWorkspace layers={layers} /></section></main>)
    const outside = screen.getByRole('button', { name: 'Outside action' })
    const trigger = screen.getByRole('button', { name: 'Controls' })
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog', { name: 'Workspace controls' })
    const done = within(dialog).getByRole('button', { name: 'Done' })

    expect(outside.hasAttribute('inert')).toBe(true)
    expect(outside.getAttribute('aria-hidden')).toBe('true')
    outside.focus()
    fireEvent.focusIn(outside)
    expect(document.activeElement).toBe(done)

    fireEvent.click(done)
    expect(outside.hasAttribute('inert')).toBe(false)
    expect(outside.getAttribute('aria-hidden')).toBe('false')
    expect(document.activeElement).toBe(trigger)
    result.unmount()
  })
})

describe('traced vector layers on the editor canvas', () => {
  const traced: WorkspaceLayer[] = [
    { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
    { id: 'svg', name: 'Traced', url: '/traced.svg', mediaType: 'image/svg+xml', role: 'image', sizeFrom: 'flat' },
    { id: 'fallbacks', name: 'Trace fallbacks', url: '/fallbacks.png', mediaType: 'image/png', role: 'overlay' },
  ]

  function loadUrl(container: HTMLElement, suffix: string, width: number, height: number) {
    for (const image of [...container.querySelectorAll('img')].filter(node => node.src.endsWith(suffix)) as HTMLImageElement[]) {
      Object.defineProperties(image, { naturalWidth: { configurable: true, value: width }, naturalHeight: { configurable: true, value: height } })
      fireEvent.load(image)
    }
  }

  it('registers a millimetre-sized SVG on the flattened pixel grid and overlays the trace audit', () => {
    const { container } = render(<ImageWorkspace layers={traced} preferredLayerId="svg" editorMode inspectionKinds={['fallbacks']} canvasLabel="Trace canvas" />)
    // A 1200 mm wide SVG reports ~4535 CSS px; the raster grid is 3000 × 1000.
    loadUrl(container, '/traced.svg', 4535, 1512)
    loadUrl(container, '/flat.png', 3000, 1000)
    loadUrl(container, '/fallbacks.png', 3000, 1000)
    const stage = container.querySelector('.image-stage') as HTMLElement
    expect(stage.style.width).toBe('3000px')
    expect(stage.style.height).toBe('1000px')
    expect(screen.getByText('Trace canvas')).toBeTruthy()
    expect(screen.getByAltText('Traced').classList.contains('vector')).toBe(true)

    fireEvent.click(within(screen.getByRole('group', { name: 'Inspection layers' })).getByLabelText('Trace fallbacks'))
    const overlay = screen.getByAltText('Trace fallbacks')
    expect(overlay.parentElement?.classList.contains('image-stage')).toBe(true)
  })

  it('offers no inspection toggles on the palette canvas', () => {
    render(<ImageWorkspace layers={traced} editorMode />)
    expect(screen.queryByRole('group', { name: 'Inspection layers' })).toBeNull()
  })

  it('shows the preferred layer, never the first, the moment the shown layer disappears', () => {
    // A trace step swaps every artifact id at once; the new overlap SVG is listed first
    // but the run's variant is the edge SVG. No render may show or fetch the overlap.
    const run = (id: string): WorkspaceLayer[] => [
      { id: `${id}-overlap`, name: `traced-overlap · ${id}`, url: `/${id}-overlap.svg`, mediaType: 'image/svg+xml', role: 'image' },
      { id: `${id}-edge`, name: `Traced SVG · ${id}`, url: `/${id}-edge.svg`, mediaType: 'image/svg+xml', role: 'image' },
    ]
    const { container, rerender } = render(<ImageWorkspace layers={run('a')} preferredLayerId="a-edge" editorMode canvasLabel="Trace canvas" />)
    expect(container.querySelector('.image-stage img')?.getAttribute('src')).toBe('/a-edge.svg')
    // Every src the shown <img> is given, including any intermediate render that the
    // effects would correct before the test could look at the settled DOM.
    const assigned: string[] = []
    const setAttribute = Element.prototype.setAttribute
    const spy = vi.spyOn(Element.prototype, 'setAttribute').mockImplementation(function (this: Element, name: string, value: string) {
      if (name === 'src' && this.parentElement?.classList.contains('image-stage')) assigned.push(value)
      return setAttribute.call(this, name, value)
    })
    rerender(<ImageWorkspace layers={run('b')} preferredLayerId="b-edge" editorMode canvasLabel="Trace canvas" />)
    spy.mockRestore()
    expect(assigned).toEqual(['/b-edge.svg'])
    expect(container.querySelector('.image-stage img')?.getAttribute('src')).toBe('/b-edge.svg')
    expect(container.querySelector('.viewport-toolbar span')?.textContent).toBe('Traced SVG · b')
  })

  it('shows a non-blocking spinner while artifacts load and until the shown image has loaded', () => {
    const { container, rerender } = render(<ImageWorkspace layers={traced} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" />)
    const busy = () => container.querySelector('.canvas-busy')
    expect(busy()?.textContent).toBe('Loading image…')
    loadUrl(container, '/traced.svg', 4535, 1512)
    expect(busy()).toBeNull()
    // A same-project revision fetch keeps the previous image visible under the spinner.
    rerender(<ImageWorkspace layers={traced} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" loading />)
    expect(busy()).not.toBeNull()
    expect(screen.getByAltText('Traced')).toBeTruthy()
    expect(container.querySelector('.viewport')?.getAttribute('aria-busy')).toBe('true')
    // The artifacts arrived; the newly shown image is still loading.
    const next = traced.map(layer => layer.id === 'svg' ? { ...layer, id: 'svg2', url: '/traced-2.svg' } : layer)
    rerender(<ImageWorkspace layers={next} preferredLayerId="svg2" editorMode canvasLabel="Trace canvas" />)
    expect(busy()).not.toBeNull()
    loadUrl(container, '/traced-2.svg', 4535, 1512)
    expect(busy()).toBeNull()
    expect(container.querySelector('.viewport')?.getAttribute('aria-busy')).toBe('false')
  })

  it('ends the spinner only when the shown URL itself settles, loaded or failed', () => {
    const withSvg = (url: string) => traced.map(layer => layer.id === 'svg' ? { ...layer, id: url, url } : layer)
    const { container, rerender } = render(<ImageWorkspace layers={withSvg('/traced.svg')} preferredLayerId="/traced.svg" editorMode canvasLabel="Trace canvas" />)
    const busy = () => container.querySelector('.canvas-busy')
    const shown = () => container.querySelector('.image-stage img') as HTMLImageElement
    const settle = (event: 'load' | 'error', currentSrc?: string) => {
      Object.defineProperty(shown(), 'currentSrc', { configurable: true, value: currentSrc === undefined ? '' : new URL(currentSrc, document.baseURI).href })
      if (event === 'load') fireEvent.load(shown()); else fireEvent.error(shown())
    }
    settle('load')
    expect(busy()).toBeNull()
    rerender(<ImageWorkspace layers={withSvg('/traced-2.svg')} preferredLayerId="/traced-2.svg" editorMode canvasLabel="Trace canvas" />)
    expect(busy()).not.toBeNull()
    // A load still reporting the previous image does not end the new one's spinner.
    settle('load', '/traced.svg')
    expect(busy()).not.toBeNull()
    settle('load', '/traced-2.svg')
    expect(busy()).toBeNull()
    // A failed request settles too, so the spinner never sticks.
    rerender(<ImageWorkspace layers={withSvg('/traced-3.svg')} preferredLayerId="/traced-3.svg" editorMode canvasLabel="Trace canvas" />)
    expect(busy()).not.toBeNull()
    settle('error', '/traced-3.svg')
    expect(busy()).toBeNull()
    // An error still reporting another image leaves the spinner up.
    rerender(<ImageWorkspace layers={withSvg('/traced-4.svg')} preferredLayerId="/traced-4.svg" editorMode canvasLabel="Trace canvas" />)
    settle('error', '/traced-3.svg')
    expect(busy()).not.toBeNull()
    settle('error')
    expect(busy()).toBeNull()
  })

  it('never registers the previous image\'s size under the newly shown layer', () => {
    const run = (id: string, url: string): WorkspaceLayer[] => [{ id, name: id, url, mediaType: 'image/png', role: 'image' }]
    const { container, rerender } = render(<ImageWorkspace layers={run('a', '/a.png')} preferredLayerId="a" editorMode />)
    const stage = () => container.querySelector('.image-stage') as HTMLElement
    const shown = () => container.querySelector('.image-stage img') as HTMLImageElement
    const loadShown = (currentSrc: string, width: number, height: number) => {
      Object.defineProperties(shown(), {
        currentSrc: { configurable: true, value: new URL(currentSrc, document.baseURI).href },
        naturalWidth: { configurable: true, value: width }, naturalHeight: { configurable: true, value: height },
      })
      fireEvent.load(shown())
    }
    loadShown('/a.png', 300, 200)
    expect(stage().style.width).toBe('300px')
    rerender(<ImageWorkspace layers={run('b', '/b.png')} preferredLayerId="b" editorMode />)
    // A late load of the previous image (its own size) must not size the new layer.
    loadShown('/a.png', 300, 200)
    loadShown('/a.png', 999, 999)
    expect(stage().style.width).not.toBe('999px')
    loadShown('/b.png', 640, 480)
    expect(stage().style.width).toBe('640px')
  })

  it('announces loading through a polite live region without adding a status role', () => {
    const { container } = render(<ImageWorkspace layers={traced} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" loading />)
    const live = container.querySelector('[aria-live="polite"]')
    expect(live?.textContent).toBe('Loading image…')
    // Outside the busy (and possibly inert) viewport, so assistive tech announces it.
    expect(live?.closest('[aria-busy], [inert]')).toBeNull()
    expect(container.querySelector('.canvas-busy')?.getAttribute('aria-hidden')).toBe('true')
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('keeps an inspection toggle on when a new run replaces the overlay of the same kind', () => {
    const run = (id: string): WorkspaceLayer[] => [
      { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
      { id: `${id}-svg`, name: 'Traced', url: `/${id}.svg`, mediaType: 'image/svg+xml', role: 'image', sizeFrom: 'flat' },
      { id: `${id}-marks`, name: 'Tightened boundaries', url: `/${id}-marks.png`, mediaType: 'image/png', role: 'overlay', kind: 'trace-fallbacks-overlay' },
    ]
    const { container, rerender } = render(<ImageWorkspace layers={run('a')} preferredLayerId="a-svg" editorMode inspectionKinds={TRACE_KINDS} canvasLabel="Trace canvas" />)
    loadUrl(container, '/flat.png', 300, 100)
    loadUrl(container, '/a.svg', 300, 100)
    loadUrl(container, '/a-marks.png', 300, 100)
    const toggle = () => within(screen.getByRole('group', { name: 'Inspection layers' })).getByLabelText('Tightened boundaries') as HTMLInputElement
    fireEvent.click(toggle())
    expect(toggle().checked).toBe(true)
    rerender(<ImageWorkspace layers={run('b')} preferredLayerId="b-svg" editorMode inspectionKinds={TRACE_KINDS} canvasLabel="Trace canvas" />)
    loadUrl(container, '/b.svg', 300, 100)
    loadUrl(container, '/b-marks.png', 300, 100)
    expect(toggle().checked).toBe(true)
    expect((screen.getByAltText('Tightened boundaries') as HTMLImageElement).getAttribute('src')).toBe('/b-marks.png')
  })

  it('hides an overlay switched on in Trace on a canvas that offers no toggle, and restores it on return', () => {
    const traced: WorkspaceLayer[] = [
      { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
      { id: 'marks', name: 'Tightened boundaries', url: '/marks.png', mediaType: 'image/png', role: 'overlay', kind: 'trace-fallbacks-overlay' },
    ]
    const { container, rerender } = render(<ImageWorkspace layers={traced} preferredLayerId="flat" editorMode inspectionKinds={TRACE_KINDS} canvasLabel="Trace canvas" />)
    loadUrl(container, '/flat.png', 300, 100)
    loadUrl(container, '/marks.png', 300, 100)
    fireEvent.click(within(screen.getByRole('group', { name: 'Inspection layers' })).getByLabelText('Tightened boundaries'))
    expect(container.querySelector('img.inspection-overlay')).not.toBeNull()
    rerender(<ImageWorkspace layers={traced} preferredLayerId="flat" editorMode canvasLabel="Flatten canvas" />)
    expect(screen.queryByRole('group', { name: 'Inspection layers' })).toBeNull()
    expect(container.querySelector('img.inspection-overlay')).toBeNull()
    rerender(<ImageWorkspace layers={traced} preferredLayerId="flat" editorMode inspectionKinds={TRACE_KINDS} canvasLabel="Trace canvas" />)
    expect(container.querySelector('img.inspection-overlay')).not.toBeNull()
  })
})

describe('canvas loading states and wheel zoom', () => {
  const single: WorkspaceLayer[] = [{ id: 'svg', name: 'Traced SVG · solid underlayers', url: '/run-8.svg', mediaType: 'image/svg+xml', role: 'image' }]

  function fitted(container: HTMLElement) {
    const viewport = container.querySelector('.viewport') as HTMLElement
    Object.defineProperties(viewport, { clientWidth: { configurable: true, value: 800 }, clientHeight: { configurable: true, value: 600 } })
    viewport.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) })
    const image = container.querySelector('.image-stage img') as HTMLImageElement
    Object.defineProperties(image, { naturalWidth: { configurable: true, value: 1000 }, naturalHeight: { configurable: true, value: 700 } })
    fireEvent.load(image)
    return viewport
  }

  it('titles the canvas as loading, never with the previous layer\'s name, while a revision loads', () => {
    const { container, rerender } = render(<ImageWorkspace layers={single} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" />)
    const title = () => container.querySelector('.viewport-toolbar span')?.textContent
    expect(title()).toBe('Traced SVG · solid underlayers')
    rerender(<ImageWorkspace layers={single} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" loading />)
    expect(title()).toBe('Loading…')
    // The old image stays underneath.
    expect(screen.getByAltText('Traced SVG · solid underlayers')).toBeTruthy()
  })

  it('keeps the Live preview switch through a load, requesting and drawing nothing until unpaused', async () => {
    vi.useFakeTimers()
    localStorage.setItem(LIVE_PREVIEW_STORAGE_KEY, 'on')
    const fetch = vi.fn(() => new Promise(() => undefined))
    vi.stubGlobal('fetch', fetch)
    try {
      const live = { revisionId: 'rev-1', settings: DEFAULT_TRACE_SETTINGS, tracedSize: { width: 1000, height: 700 } }
      const view = (paused: boolean) => <ImageWorkspace layers={single} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" livePreview={live} livePreviewPaused={paused} />
      const { container, rerender } = render(view(true))
      fitted(container)
      fireEvent.click(screen.getByLabelText('Zoom in'))
      fireEvent.click(screen.getByLabelText('Zoom in'))
      act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS * 4) })
      expect(screen.getByRole('switch', { name: 'Live preview' })).toBeTruthy()
      expect(fetch).not.toHaveBeenCalled()
      expect(screen.queryByText('Zoom in to preview')).toBeNull()
      rerender(view(false))
      fireEvent.click(screen.getByLabelText('Zoom in'))
      act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS * 4) })
      expect(fetch).toHaveBeenCalledTimes(1)
    } finally { localStorage.clear(); vi.useRealTimers() }
  })

  it('renders pixels as crisp blocks only once zoomed to 1:1 or closer, smoothing below', () => {
    const { container } = render(<ImageWorkspace layers={single} preferredLayerId="svg" editorMode canvasLabel="Upload canvas" />)
    const viewport = fitted(container)
    const stage = container.querySelector('.image-stage') as HTMLElement
    // 1000 × 700 fitted into 800 × 600 is below 1:1.
    expect(stage.classList.contains('magnified')).toBe(false)
    for (let step = 0; step < 20 && !stage.classList.contains('magnified'); step++) {
      act(() => { viewport.dispatchEvent(new WheelEvent('wheel', { deltaY: -100, clientX: 400, clientY: 300, bubbles: true, cancelable: true })) })
    }
    expect(stage.classList.contains('magnified')).toBe(true)
    expect(Number(/scale\(([\d.]+)\)/.exec(stage.style.transform)?.[1])).toBeGreaterThanOrEqual(1)
  })

  it('zooms on the wheel through a non-passive listener that cancels the page scroll', () => {
    const added = vi.spyOn(HTMLElement.prototype, 'addEventListener')
    const { container } = render(<ImageWorkspace layers={single} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" />)
    const viewport = fitted(container)
    // The viewport's own wheel listener (React's root listener is passive).
    const registrations = added.mock.calls.filter((call, index) => call[0] === 'wheel' && added.mock.contexts[index] === viewport)
    expect(registrations.map(call => call[2])).toEqual([{ passive: false }])
    added.mockRestore()
    const stage = container.querySelector('.image-stage') as HTMLElement
    const before = stage.style.transform
    const event = new WheelEvent('wheel', { deltaY: -100, clientX: 400, clientY: 300, bubbles: true, cancelable: true })
    act(() => { viewport.dispatchEvent(event) })
    expect(event.defaultPrevented).toBe(true)
    expect(stage.style.transform).not.toBe(before)
  })

  it('titles a real step as App drives it: old run, loading, then only the new run', () => {
    // The props App hands the canvas across a step from run 9 (edge to edge) to run 8
    // (solid underlayers): the revision changes (loading, old layers), the fetch runs,
    // the new layers commit, then loading ends.
    // Named and identified as App builds them: each run lists both SVG variants; the
    // run's own variant carries the settings name (tracedSvgKind picks it).
    const run = (id: string, shown: 'edge' | 'overlap', name: string): WorkspaceLayer[] => [
      { id: 'source-id', name: 'source · source.png', url: '/source.png', mediaType: 'image/png', role: 'image' },
      { id: `${id}-edge`, name: shown === 'edge' ? name : 'traced-edge · traced-edge.svg', url: `/${id}-edge.svg`, mediaType: 'image/svg+xml', role: 'image' },
      { id: `${id}-overlap`, name: shown === 'overlap' ? name : 'traced-overlap · traced-overlap.svg', url: `/${id}-overlap.svg`, mediaType: 'image/svg+xml', role: 'image' },
    ]
    const steps: [WorkspaceLayer[], string, boolean][] = [
      [run('trace-9', 'edge', 'Traced SVG · edge to edge'), 'trace-9-edge', false],
      [run('trace-9', 'edge', 'Traced SVG · edge to edge'), 'trace-9-edge', true],
      [run('trace-8', 'overlap', 'Traced SVG · solid underlayers'), 'trace-8-overlap', true],
      [run('trace-8', 'overlap', 'Traced SVG · solid underlayers'), 'trace-8-overlap', false],
    ]
    const { container, rerender } = render(<ImageWorkspace layers={steps[0][0]} preferredLayerId={steps[0][1]} editorMode canvasLabel="Trace canvas" loading={steps[0][2]} />)
    const titles = [container.querySelector('.viewport-toolbar span')?.textContent]
    for (const [layers, preferred, loading] of steps.slice(1)) {
      rerender(<ImageWorkspace layers={layers} preferredLayerId={preferred} editorMode canvasLabel="Trace canvas" loading={loading} />)
      titles.push(container.querySelector('.viewport-toolbar span')?.textContent)
    }
    expect(titles).toEqual(['Traced SVG · edge to edge', 'Loading…', 'Loading…', 'Traced SVG · solid underlayers'])
  })

  it('attaches the wheel listener once the viewport first appears, and removes it on unmount', () => {
    const added = vi.spyOn(HTMLElement.prototype, 'addEventListener')
    const removed = vi.spyOn(HTMLElement.prototype, 'removeEventListener')
    const { container, rerender, unmount } = render(<ImageWorkspace layers={[]} editorMode canvasLabel="Trace canvas" />)
    expect(container.querySelector('.viewport')).toBeNull()
    rerender(<ImageWorkspace layers={single} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" />)
    const viewport = fitted(container)
    const wheelOn = (spy: typeof added) => spy.mock.calls.filter((call, index) => call[0] === 'wheel' && spy.mock.contexts[index] === viewport)
    expect(wheelOn(added)).toHaveLength(1)
    const listener = wheelOn(added)[0][1]
    const event = new WheelEvent('wheel', { deltaY: -100, clientX: 400, clientY: 300, bubbles: true, cancelable: true })
    act(() => { viewport.dispatchEvent(event) })
    expect(event.defaultPrevented).toBe(true)
    unmount()
    expect(wheelOn(removed).map(call => call[1])).toEqual([listener])
    added.mockRestore(); removed.mockRestore()
  })

  it('keeps the switch with no traced grid yet, requesting nothing and saying why when told', () => {
    vi.useFakeTimers()
    localStorage.setItem(LIVE_PREVIEW_STORAGE_KEY, 'on')
    const fetch = vi.fn(() => new Promise(() => undefined))
    vi.stubGlobal('fetch', fetch)
    try {
      const live = { revisionId: 'rev-1', settings: DEFAULT_TRACE_SETTINGS, tracedSize: null }
      const { container } = render(<ImageWorkspace layers={single} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" livePreview={live} livePreviewStatus="Preview unavailable: no traced grid" />)
      fitted(container)
      fireEvent.click(screen.getByLabelText('Zoom in'))
      fireEvent.click(screen.getByLabelText('Zoom in'))
      act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS * 4) })
      expect(screen.getByRole('switch', { name: 'Live preview' })).toBeTruthy()
      expect(fetch).not.toHaveBeenCalled()
      const status = container.querySelector('.trace-preview-paused')
      expect(status?.getAttribute('aria-live')).toBe('polite')
      expect(status?.textContent).toBe('Preview unavailable: no traced grid')
    } finally { localStorage.clear(); vi.useRealTimers() }
  })
})

describe('preservation markers, pick aim and upscaled overlays on the Flatten canvas', () => {
  // A 100 × 50 source flattened at 2× upscale onto a 200 × 100 grid, with the flatten's
  // protected-areas overlay on that flattened grid.
  const flatten: WorkspaceLayer[] = [
    { id: 'source', name: 'Source', url: '/source.png', mediaType: 'image/png', role: 'image' },
    { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
    { id: 'protected', name: 'Protected areas', url: '/protected.png', mediaType: 'image/png', role: 'overlay', kind: 'protected-areas-overlay' },
    { id: 'missed', name: 'Missed source details (red)', url: '/missed.png', mediaType: 'image/png', role: 'overlay', kind: 'trace-missed-details-overlay' },
  ]
  const markers: CanvasMarker[] = [
    { id: 'protect-0', kind: 'protect', label: '1', points: [[10, 20]] },
    { id: 'preserve-0', kind: 'preserve', label: '1', points: [[30, 5]] },
    { id: 'preserve-1', kind: 'preserve', label: '2', points: [[40, 6]] },
    { id: 'pair-0', kind: 'pair', label: '1', points: [[2, 3], [80, 40]] },
    { id: 'pair-pending', kind: 'pair', label: '2', points: [[60, 10]], pending: true },
  ]
  type Props = Partial<Parameters<typeof ImageWorkspace>[0]>
  const canvas = (props: Props) => <ImageWorkspace layers={flatten} editorMode canvasLabel="Flatten canvas" markers={markers} markerGridFrom="source" inspectionKinds={['protected-areas-overlay']} {...props} />

  function prepareFlatten(props: Props = {}) {
    const result = render(canvas(props))
    const viewport = within(result.container).getByLabelText(props.selectionLabel ?? 'Image comparison viewport')
    Object.defineProperties(viewport, { clientWidth: { configurable: true, value: 800 }, clientHeight: { configurable: true, value: 600 } })
    viewport.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) })
    HTMLElement.prototype.setPointerCapture = () => undefined
    for (const [name, width, height] of [['source', 100, 50], ['flat', 200, 100], ['protected', 200, 100], ['missed', 200, 100]] as const) {
      for (const image of [...result.container.querySelectorAll('img')].filter(node => node.src.endsWith(`/${name}.png`)) as HTMLImageElement[]) {
        Object.defineProperties(image, { naturalWidth: { configurable: true, value: width }, naturalHeight: { configurable: true, value: height } })
        fireEvent.load(image)
      }
    }
    return { ...result, viewport }
  }
  const view = (container: HTMLElement) => {
    const match = /translate3d\(([-\d.e]+)px, ([-\d.e]+)px, 0\) scale\(([\d.e]+)\)/.exec((container.querySelector('.image-stage') as HTMLElement).style.transform)!
    return { x: Number(match[1]), y: Number(match[2]), scale: Number(match[3]) }
  }
  const pointsOf = (container: HTMLElement, id: string) => [...container.querySelectorAll(`[data-marker-id="${id}"] .marker-point`)].map(node => {
    const match = /translate\(([-\d.e]+) ([-\d.e]+)\)/.exec(node.getAttribute('transform')!)!
    return { x: Number(match[1]), y: Number(match[2]) }
  })
  const expectAt = (actual: { x: number; y: number }, expected: { x: number; y: number }) => {
    expect(actual.x).toBeCloseTo(expected.x, 6)
    expect(actual.y).toBeCloseTo(expected.y, 6)
  }

  it('draws each kind as its own numbered shape on its pixel centre, hidden from assistive tech', () => {
    const { container } = prepareFlatten({ preferredLayerId: 'source' })
    const layer = container.querySelector('svg.canvas-annotations')!
    expect(layer.getAttribute('aria-hidden')).toBe('true')
    const marker = (id: string) => container.querySelector(`[data-marker-id="${id}"]`)!
    expect(marker('protect-0').classList.contains('protect')).toBe(true)
    expect(marker('protect-0').querySelector('.marker-shape')?.tagName).toBe('polygon')
    expect(marker('preserve-0').querySelector('.marker-shape')?.tagName).toBe('circle')
    expect(marker('pair-0').querySelector('.marker-point .marker-shape')?.tagName).toBe('rect')
    // A pair is two markers joined by a line.
    expect(pointsOf(container, 'pair-0')).toHaveLength(2)
    expect(marker('pair-0').querySelectorAll('.marker-link line')).toHaveLength(2)
    expect([...marker('preserve-1').querySelectorAll('.marker-label')].map(node => node.textContent)).toEqual(['2'])
    expect([...marker('pair-0').querySelectorAll('.marker-label')].map(node => node.textContent)).toEqual(['1', '1'])
    const { x, y, scale } = view(container)
    expectAt(pointsOf(container, 'protect-0')[0], { x: x + 10.5 * scale, y: y + 20.5 * scale })
    expectAt(pointsOf(container, 'pair-0')[1], { x: x + 80.5 * scale, y: y + 40.5 * scale })
  })

  it('follows zoom and pan while keeping a constant on-screen size', () => {
    const { container, viewport } = prepareFlatten({ preferredLayerId: 'source' })
    const radius = () => container.querySelector('[data-marker-id="preserve-0"] .marker-shape')!.getAttribute('r')
    const before = view(container)
    const initialRadius = radius()
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 160, clientY: 130 })
    fireEvent.pointerUp(viewport, { pointerId: 1, clientX: 160, clientY: 130 })
    const after = view(container)
    expect(after.scale).toBeCloseTo(before.scale * 1.5625, 6)
    expectAt(pointsOf(container, 'preserve-0')[0], { x: after.x + 30.5 * after.scale, y: after.y + 5.5 * after.scale })
    expect(radius()).toBe(initialRadius)
  })

  it('scales source-grid markers onto a larger flattened view', () => {
    const { container } = prepareFlatten({ preferredLayerId: 'flat' })
    expect((container.querySelector('.image-stage') as HTMLElement).style.width).toBe('200px')
    const { x, y, scale } = view(container)
    // Source pixel (10, 20) covers flattened pixels 20–21 × 40–41; its centre is 21, 41.
    expectAt(pointsOf(container, 'protect-0')[0], { x: x + 21 * scale, y: y + 41 * scale })
    expectAt(pointsOf(container, 'pair-pending')[0], { x: x + 121 * scale, y: y + 21 * scale })
  })

  it('draws no markers over an image whose grid does not register with theirs', () => {
    const odd: WorkspaceLayer[] = [{ id: 'report', name: 'Report', url: '/report.png', mediaType: 'image/png', role: 'image' }, ...flatten]
    const { container } = render(<ImageWorkspace layers={odd} editorMode preferredLayerId="report" markers={markers} markerGridFrom="source" />)
    for (const [name, width, height] of [['report', 100, 70], ['source', 100, 50]] as const) {
      const image = [...container.querySelectorAll('img')].find(node => node.src.endsWith(`/${name}.png`)) as HTMLImageElement
      Object.defineProperties(image, { naturalWidth: { configurable: true, value: width }, naturalHeight: { configurable: true, value: height } })
      fireEvent.load(image)
    }
    expect(container.querySelector('.canvas-marker')).toBeNull()
  })

  it('shows the pending pair marker and highlights the marker of the hovered row', () => {
    const { container, rerender } = prepareFlatten({ preferredLayerId: 'source' })
    const marker = (id: string) => container.querySelector(`[data-marker-id="${id}"]`)!
    expect(marker('pair-pending').classList.contains('pending')).toBe(true)
    expect(marker('pair-0').classList.contains('pending')).toBe(false)
    expect(container.querySelector('.canvas-marker.highlighted')).toBeNull()
    rerender(canvas({ preferredLayerId: 'source', highlightedMarker: 'preserve-1' }))
    expect([...container.querySelectorAll('.canvas-marker.highlighted')].map(node => node.getAttribute('data-marker-id'))).toEqual(['preserve-1'])
  })

  it('aims a pick with a crosshair on the hovered pixel and reads out its coordinates', () => {
    const onSelectPoint = vi.fn()
    const label = 'Source image preservation-point selector'
    const { container, viewport } = prepareFlatten({ preferredLayerId: 'source', onSelectPoint, pickAim: true, selectionLabel: label })
    const { x, y, scale } = view(container)
    const client = { clientX: x + 12.5 * scale, clientY: y + 34.5 * scale }
    fireEvent.pointerMove(viewport, { pointerId: 1, ...client })
    const square = container.querySelector('.pick-aim .marker-shape .pick-hover')!
    expect(Number(square.getAttribute('x')) + Number(square.getAttribute('width')) / 2).toBeCloseTo(x + 12.5 * scale, 6)
    expect(Number(square.getAttribute('y')) + Number(square.getAttribute('height')) / 2).toBeCloseTo(y + 34.5 * scale, 6)
    expect(container.querySelectorAll('.pick-aim .marker-shape line')).toHaveLength(4)
    const readout = container.querySelector('.pick-readout')!
    expect(readout.textContent).toBe('x 12, y 34')
    expect(readout.getAttribute('aria-hidden')).toBe('true')
    // The annotations take no pointer: a click on a marker's pixel still picks it.
    fireEvent.pointerDown(viewport, { pointerId: 1, ...client })
    fireEvent.pointerUp(viewport, { pointerId: 1, ...client })
    expect(onSelectPoint).toHaveBeenCalledWith([12, 34])
    fireEvent.pointerLeave(viewport)
    expect(container.querySelector('.pick-aim')).toBeNull()
    expect(container.querySelector('.pick-readout')).toBeNull()
  })

  it('shows no aim or readout on the Edit stage area selector, which picks without aiming', () => {
    const onSelectPoint = vi.fn()
    const label = 'Flattened artwork area selector'
    const { container, viewport } = prepareFlatten({ preferredLayerId: 'flat', onSelectPoint, selectionLabel: label, canvasLabel: 'Edit canvas', markers: [], inspectionKinds: [] })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 400, clientY: 300 })
    expect(container.querySelector('.pick-aim')).toBeNull()
    expect(container.querySelector('.pick-readout')).toBeNull()
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 400, clientY: 300 })
    fireEvent.pointerUp(viewport, { pointerId: 1, clientX: 400, clientY: 300 })
    expect(onSelectPoint).toHaveBeenCalledTimes(1)
  })

  it('flips the readout by its measured size near the viewport edge, anchoring its far corner', () => {
    const measured = { offsetWidth: 150, offsetHeight: 20 }
    const original = Object.getOwnPropertyDescriptors(HTMLElement.prototype)
    for (const [name, size] of Object.entries(measured)) {
      Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get(this: HTMLElement) { return this.classList.contains('pick-readout') ? size : 0 } })
    }
    try {
      const label = 'Source image preservation-point selector'
      const { container, viewport } = prepareFlatten({ preferredLayerId: 'source', onSelectPoint: vi.fn(), pickAim: true, selectionLabel: label })
      const readout = () => container.querySelector('.pick-readout') as HTMLElement
      fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 400, clientY: 300 })
      expect(readout().style.transform).toBe('')
      // 670 px across: a 150 px readout would cross the 800 px edge (a 110 px one would not).
      fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 670, clientY: 300 })
      expect(readout().style.transform).toBe('translate(-100%, 0)')
      const aim = container.querySelector('.pick-aim .marker-shape .pick-hover')!
      const centre = Number(aim.getAttribute('x')) + Number(aim.getAttribute('width')) / 2
      expect(parseFloat(readout().style.left)).toBeLessThan(centre)
    } finally {
      for (const name of Object.keys(measured)) {
        if (original[name]) Object.defineProperty(HTMLElement.prototype, name, original[name])
        else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[name]
      }
    }
  })

  it('draws a Trace audit overlay over the Source view when the traced grid is an upscale of it', () => {
    const { container } = prepareFlatten({ preferredLayerId: 'source', canvasLabel: 'Trace canvas', inspectionKinds: TRACE_KINDS, markers: [] })
    fireEvent.click(within(screen.getByRole('group', { name: 'Inspection layers' })).getByLabelText('Missed source details (red)'))
    // The 200 × 100 traced-grid overlay stretches over the 100 × 50 source.
    expect([...container.querySelectorAll('img.inspection-overlay')].map(node => node.getAttribute('alt'))).toEqual(['Missed source details (red)'])
    expect(screen.queryByText(/Hidden because its pixel dimensions differ/)).toBeNull()
  })

  it('shows no aim outside pick mode', () => {
    const { container, viewport } = prepareFlatten({ preferredLayerId: 'source' })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 400, clientY: 300 })
    expect(container.querySelector('.pick-aim')).toBeNull()
    expect(container.querySelector('.pick-readout')).toBeNull()
  })

  it('offers only its own overlay and draws it on both the source and the upscaled flattened view', () => {
    const { container, rerender } = prepareFlatten({ preferredLayerId: 'source' })
    const group = screen.getByRole('group', { name: 'Inspection layers' })
    expect(within(group).queryByLabelText('Missed source details (red)')).toBeNull()
    fireEvent.click(within(group).getByLabelText('Protected areas'))
    const drawn = () => [...container.querySelectorAll('img.inspection-overlay')].map(node => node.getAttribute('alt'))
    // The 200 × 100 overlay stretches over the 100 × 50 source it registers with.
    expect(drawn()).toEqual(['Protected areas'])
    expect(container.querySelector('img.inspection-overlay')?.parentElement?.classList.contains('image-stage')).toBe(true)
    expect(screen.queryByText(/Hidden because its pixel dimensions differ/)).toBeNull()
    rerender(canvas({ preferredLayerId: 'flat' }))
    expect(drawn()).toEqual(['Protected areas'])
    // The Trace canvas offers its own audits and never Protected areas, which stays on for Flatten.
    rerender(canvas({ preferredLayerId: 'flat', canvasLabel: 'Trace canvas', inspectionKinds: TRACE_KINDS }))
    const traceGroup = screen.getByRole('group', { name: 'Inspection layers' })
    expect(within(traceGroup).queryByLabelText('Protected areas')).toBeNull()
    expect(within(traceGroup).getByLabelText('Missed source details (red)')).toBeTruthy()
    expect(drawn()).toEqual([])
    rerender(canvas({ preferredLayerId: 'source' }))
    expect(drawn()).toEqual(['Protected areas'])
  })
})
