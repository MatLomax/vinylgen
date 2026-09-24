// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'
import { DEFAULT_TRACE_SETTINGS } from './PipelineControls'
import { LIVE_PREVIEW_STORAGE_KEY, PREVIEW_DEBOUNCE_MS, visibleTraceWindow, type LivePreviewOptions } from './TracePreview'

const layers: WorkspaceLayer[] = [
  { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
]
const TRACE_KINDS = ['trace-missed-details-overlay', 'trace-fallbacks-overlay']

type Pending = { url: string; init: RequestInit; resolve: (response: unknown) => void }
let pending: Pending[] = []

beforeEach(() => {
  pending = []
  localStorage.clear()
  vi.useFakeTimers()
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit) => new Promise(resolve => pending.push({ url, init, resolve }))))
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function workspace(livePreview?: LivePreviewOptions) {
  return <ImageWorkspace layers={layers} editorMode canvasLabel="Trace canvas" inspectionKinds={TRACE_KINDS} livePreview={livePreview} />
}

// Renders a 1000 x 700 image in an 800 x 600 viewport, fitted (scale 0.768).
function prepare(livePreview?: LivePreviewOptions) {
  const result = render(workspace(livePreview))
  const viewport = within(result.container).getByLabelText('Image comparison viewport')
  Object.defineProperties(viewport, {
    clientWidth: { configurable: true, value: 800 },
    clientHeight: { configurable: true, value: 600 },
  })
  const image = result.container.querySelector('.image-stage img') as HTMLImageElement
  Object.defineProperties(image, { naturalWidth: { configurable: true, value: 1000 }, naturalHeight: { configurable: true, value: 700 } })
  fireEvent.load(image)
  return result
}

const options = (overrides: Partial<LivePreviewOptions> = {}): LivePreviewOptions => ({
  revisionId: 'rev-1', settings: DEFAULT_TRACE_SETTINGS, tracedSize: { width: 1000, height: 700 }, ...overrides,
})

function zoomTwice() {
  // Two 1.25x zooms about the centre: scale 1.2, origin (-200, -120).
  fireEvent.click(screen.getByLabelText('Zoom in'))
  fireEvent.click(screen.getByLabelText('Zoom in'))
}

function requestBody(entry: Pending) {
  return JSON.parse(String(entry.init.body)) as { window: { x: number; y: number; width: number; height: number }; trace: Record<string, unknown> }
}

async function respond(entry: Pending, body: unknown, ok = true) {
  await act(async () => {
    entry.resolve({ ok, statusText: ok ? 'OK' : 'Conflict', json: async () => body, text: async () => JSON.stringify(body) })
  })
}

describe('visibleTraceWindow', () => {
  it('maps the viewport through the pan/zoom into clamped traced px', () => {
    expect(visibleTraceWindow({ x: -200, y: -120, scale: 1.2 }, { width: 800, height: 600 }, { width: 1000, height: 700 }, { width: 1000, height: 700 }))
      .toEqual({ x: 166, y: 100, width: 668, height: 500 })
    // A traced grid twice the displayed image (a flatten upscale) doubles the window.
    expect(visibleTraceWindow({ x: -200, y: -120, scale: 1.2 }, { width: 800, height: 600 }, { width: 1000, height: 700 }, { width: 2000, height: 1400 }))
      .toEqual({ x: 333, y: 200, width: 1334, height: 1000 })
    // Off to the right of the image: nothing in view.
    expect(visibleTraceWindow({ x: -5000, y: 0, scale: 1 }, { width: 800, height: 600 }, { width: 1000, height: 700 }, { width: 1000, height: 700 })).toBeNull()
  })
})

describe('Trace canvas live preview', () => {
  it('shows the switch only on a canvas given live-preview options', () => {
    prepare(options())
    expect(screen.getByRole('switch', { name: 'Live preview' })).toBeTruthy()
    cleanup()
    prepare()
    expect(screen.queryByRole('switch', { name: 'Live preview' })).toBeNull()
  })

  it('asks to zoom in while the view is larger than the preview limit, without requesting', () => {
    prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS * 4) })
    expect(screen.getByText('Zoom in to preview')).toBeTruthy()
    expect(pending).toHaveLength(0)
  })

  it('previews the zoomed-in window once the view settles and draws it at that window', async () => {
    const { container } = prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS - 1) })
    expect(pending).toHaveLength(0)
    act(() => { vi.advanceTimersByTime(1) })
    expect(pending).toHaveLength(1)
    expect(pending[0].url).toBe('/api/revisions/rev-1/trace-preview')
    expect(requestBody(pending[0])).toEqual({ window: { x: 166, y: 100, width: 668, height: 500 }, trace: DEFAULT_TRACE_SETTINGS })
    expect(screen.getByText('Updating live preview…')).toBeTruthy()
    const window = { x: 166, y: 100, width: 668, height: 500 }
    await respond(pending[0], { window, crop: window, image: { width: 1000, height: 700 }, variant: 'traced-edge', svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="166 100 668 500"/>', elapsed_ms: 12 })
    const shapes = screen.getByAltText('Live trace preview') as HTMLImageElement
    // Inside the transformed stage, so it follows pan and zoom with the image itself.
    expect(shapes.parentElement?.classList.contains('image-stage')).toBe(true)
    expect(shapes.style.left).toBe('166px')
    expect(shapes.style.top).toBe('100px')
    expect(shapes.style.width).toBe('668px')
    expect(shapes.style.height).toBe('500px')
    expect(shapes.src.startsWith('data:image/svg+xml')).toBe(true)
    expect(screen.getByText('Live preview', { selector: '.trace-preview-badge' })).toBeTruthy()
    expect(container.querySelector('.trace-preview-badge.is-busy')).toBeNull()
  })

  it('places a traced-grid window on the displayed image scale', async () => {
    prepare(options({ tracedSize: { width: 2000, height: 1400 } }))
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    // At scale 1.2 the view covers 668 x 500 displayed px, 1336 x 1000 traced px: too
    // large to preview. Three more zooms (scale 2.34) bring it under the limit.
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    expect(pending).toHaveLength(0)
    expect(screen.getByText('Zoom in to preview')).toBeTruthy()
    for (let step = 0; step < 3; step++) fireEvent.click(screen.getByLabelText('Zoom in'))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    const window = requestBody(pending[0]).window
    expect(window.width * window.height).toBeLessThanOrEqual(600 * 600)
    await respond(pending[0], { window, crop: window, image: { width: 2000, height: 1400 }, variant: 'traced-edge', svg: '<svg/>', elapsed_ms: 1 })
    const shapes = screen.getByAltText('Live trace preview') as HTMLImageElement
    expect(parseFloat(shapes.style.left)).toBeCloseTo(window.x / 2)
    expect(parseFloat(shapes.style.width)).toBeCloseTo(window.width / 2)
  })

  it('debounces setting changes, aborts the request in flight and ignores its late response', async () => {
    const { rerender } = prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    expect(pending).toHaveLength(1)
    const first = pending[0]
    rerender(workspace(options({ settings: { ...DEFAULT_TRACE_SETTINGS, curve_fit_tolerance: 1.1 } })))
    expect(first.init.signal?.aborted).toBe(true)
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS / 2) })
    rerender(workspace(options({ settings: { ...DEFAULT_TRACE_SETTINGS, curve_fit_tolerance: 0.9 } })))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS - 1) })
    expect(pending).toHaveLength(1)
    act(() => { vi.advanceTimersByTime(1) })
    // The intermediate 1.1 change never reached the server.
    expect(pending).toHaveLength(2)
    expect(requestBody(pending[1]).trace.curve_fit_tolerance).toBe(0.9)
    const window = requestBody(pending[1]).window
    await respond(first, { window, crop: window, image: { width: 1000, height: 700 }, variant: 'traced-edge', svg: '<svg id="stale"/>', elapsed_ms: 1 })
    expect(screen.queryByAltText('Live trace preview')).toBeNull()
    await respond(pending[1], { window, crop: window, image: { width: 1000, height: 700 }, variant: 'traced-edge', svg: '<svg id="fresh"/>', elapsed_ms: 1 })
    expect(decodeURIComponent((screen.getByAltText('Live trace preview') as HTMLImageElement).src)).toContain('fresh')
  })

  it('never shows a proxy error page, only a short server error', async () => {
    prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    const page = '<!DOCTYPE html><html><body><div class="code">503</div></body></html>'
    await act(async () => {
      pending[0].resolve({ ok: false, status: 503, statusText: 'Service Unavailable', json: async () => { throw new Error('not JSON') }, text: async () => page })
    })
    const status = screen.getByRole('status').textContent ?? ''
    expect(status).toBe('Preview failed: Server error 503 (Service Unavailable)')
    expect(status).not.toContain('<')
  })

  it('shows the server error when a preview fails', async () => {
    prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(pending[0], { detail: 'the artwork changed after Cleanup: re-run Cleanup before tracing' }, false)
    expect(screen.getByRole('status').textContent).toBe('Preview failed: the artwork changed after Cleanup: re-run Cleanup before tracing')
  })

  it('hides the previous shapes under an error and shows the busy badge while retrying', async () => {
    const { rerender, container } = prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    const window = requestBody(pending[0]).window
    const ok = (svg: string) => ({ window, crop: window, image: { width: 1000, height: 700 }, variant: 'traced-edge', svg, elapsed_ms: 1 })
    await respond(pending[0], ok('<svg id="first"/>'))
    expect(screen.getByAltText('Live trace preview')).toBeTruthy()
    // A field momentarily holding an in-progress 0 is rejected by the server.
    rerender(workspace(options({ settings: { ...DEFAULT_TRACE_SETTINGS, curve_fit_tolerance: 0 } })))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(pending[1], { detail: 'The trace settings are not valid for a preview' }, false)
    expect(screen.queryByAltText('Live trace preview')).toBeNull()
    expect(screen.getByText(/Preview failed/)).toBeTruthy()
    rerender(workspace(options({ settings: { ...DEFAULT_TRACE_SETTINGS, curve_fit_tolerance: 0.5 } })))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    expect(container.querySelector('.trace-preview-badge.is-busy')?.textContent).toBe('Updating live preview…')
    expect(screen.queryByAltText('Live trace preview')).toBeNull()
    await respond(pending[2], ok('<svg id="second"/>'))
    expect(decodeURIComponent((screen.getByAltText('Live trace preview') as HTMLImageElement).src)).toContain('second')
    expect(screen.queryByText(/Preview failed/)).toBeNull()
  })

  it('drops an earlier error on zooming out, and on the next request after zooming back in', async () => {
    prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(pending[0], { detail: 'The trace settings are not valid for a preview' }, false)
    const status = screen.getByRole('status')
    expect(status.textContent).toContain('Preview failed')
    fireEvent.click(screen.getByLabelText('Zoom out'))
    fireEvent.click(screen.getByLabelText('Zoom out'))
    expect(status.textContent).toBe('Zoom in to preview')
    zoomTwice()
    // Back in view: the old failure does not come back while the new request waits.
    expect(status.textContent).toBe('')
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    expect(pending).toHaveLength(2)
    expect(status.textContent).toBe('Updating live preview…')
  })

  it('drops an earlier error once the view leaves the image', async () => {
    const { container } = prepare(options())
    HTMLElement.prototype.setPointerCapture = () => undefined
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(pending[0], { detail: 'bad value' }, false)
    const status = screen.getByRole('status')
    expect(status.textContent).toBe('Preview failed: bad value')
    const viewport = within(container).getByLabelText('Image comparison viewport')
    // Drag the image entirely off to the right: nothing of it is in view.
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 0, clientY: 300 })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 5000, clientY: 300 })
    expect(status.textContent).toBe('')
    // Dragging it back does not resurrect the old failure.
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 0, clientY: 300 })
    fireEvent.pointerUp(viewport, { pointerId: 1, clientX: 0, clientY: 300 })
    expect(status.textContent).toBe('')
    expect(pending).toHaveLength(1)
  })

  it('clears an error as soon as the next request starts', async () => {
    const { rerender } = prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(pending[0], { detail: 'bad value' }, false)
    expect(screen.getByRole('status').textContent).toBe('Preview failed: bad value')
    rerender(workspace(options({ settings: { ...DEFAULT_TRACE_SETTINGS, curve_fit_tolerance: 0.5 } })))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    expect(screen.getByRole('status').textContent).toBe('Updating live preview…')
  })

  it('lets a pan start over the badge but not over the switch', async () => {
    const { container } = prepare(options())
    HTMLElement.prototype.setPointerCapture = () => undefined
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    const window = requestBody(pending[0]).window
    await respond(pending[0], { window, crop: window, image: { width: 1000, height: 700 }, variant: 'traced-edge', svg: '<svg/>', elapsed_ms: 1 })
    const viewport = within(container).getByLabelText('Image comparison viewport')
    const stage = container.querySelector('.image-stage') as HTMLElement
    const before = stage.style.transform
    const badge = container.querySelector('.trace-preview-badge') as HTMLElement
    fireEvent.pointerDown(badge, { pointerId: 1, clientX: 30, clientY: 30 })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 80, clientY: 60 })
    fireEvent.pointerUp(viewport, { pointerId: 1, clientX: 80, clientY: 60 })
    expect(stage.style.transform).not.toBe(before)
    const panned = stage.style.transform
    fireEvent.pointerDown(screen.getByText('Live preview', { selector: 'label' }), { pointerId: 2, clientX: 20, clientY: 20 })
    fireEvent.pointerMove(viewport, { pointerId: 2, clientX: 90, clientY: 90 })
    fireEvent.pointerUp(viewport, { pointerId: 2, clientX: 90, clientY: 90 })
    expect(stage.style.transform).toBe(panned)
  })

  it('remembers the switch across remounts and reloads', () => {
    prepare(options())
    expect((screen.getByRole('switch', { name: 'Live preview' }) as HTMLInputElement).checked).toBe(false)
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    expect(localStorage.getItem(LIVE_PREVIEW_STORAGE_KEY)).toBe('on')
    cleanup()
    prepare(options())
    expect((screen.getByRole('switch', { name: 'Live preview' }) as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    expect(localStorage.getItem(LIVE_PREVIEW_STORAGE_KEY)).toBe('off')
  })

  it('announces only meaningful states, not the idle label', async () => {
    const { container } = prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    const status = screen.getByRole('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toBe('Zoom in to preview')
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    expect(status.textContent).toBe('Updating live preview…')
    const badge = container.querySelector('.trace-preview-badge') as HTMLElement
    expect(status.contains(badge)).toBe(true)
    expect(badge.closest('[aria-hidden="true"]')).toBeNull()
    // Only the spinner is decorative.
    expect(badge.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true')
    // The trace preview has no busy note to read: its only request is the preview.
    expect(pending).toHaveLength(1)
    const window = requestBody(pending[0]).window
    await respond(pending[0], { window, crop: window, image: { width: 1000, height: 700 }, variant: 'traced-edge', svg: '<svg/>', elapsed_ms: 1 })
    expect(screen.getByRole('status')).toBe(status)
    // Idle: the "Live preview" label stays visible but is not announced.
    expect(status.textContent).toBe('')
    const idleBadge = container.querySelector('.trace-preview-badge') as HTMLElement
    expect(idleBadge.textContent).toBe('Live preview')
    expect(status.contains(idleBadge)).toBe(false)
    expect(idleBadge.getAttribute('aria-hidden')).toBe('true')
  })
})
