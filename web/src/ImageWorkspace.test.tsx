// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'

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
    const { container } = render(<ImageWorkspace layers={traced} preferredLayerId="svg" editorMode inspectionToggles canvasLabel="Trace canvas" />)
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
    const { container, rerender } = render(<ImageWorkspace layers={run('a')} preferredLayerId="a-svg" editorMode inspectionToggles canvasLabel="Trace canvas" />)
    loadUrl(container, '/flat.png', 300, 100)
    loadUrl(container, '/a.svg', 300, 100)
    loadUrl(container, '/a-marks.png', 300, 100)
    const toggle = () => within(screen.getByRole('group', { name: 'Inspection layers' })).getByLabelText('Tightened boundaries') as HTMLInputElement
    fireEvent.click(toggle())
    expect(toggle().checked).toBe(true)
    rerender(<ImageWorkspace layers={run('b')} preferredLayerId="b-svg" editorMode inspectionToggles canvasLabel="Trace canvas" />)
    loadUrl(container, '/b.svg', 300, 100)
    loadUrl(container, '/b-marks.png', 300, 100)
    expect(toggle().checked).toBe(true)
    expect((screen.getByAltText('Tightened boundaries') as HTMLImageElement).getAttribute('src')).toBe('/b-marks.png')
  })
})
