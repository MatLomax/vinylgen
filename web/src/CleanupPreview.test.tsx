// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CleanupSettings } from './api'
import { CLEANUP_CHANGES_STORAGE_KEY, CLEANUP_PREVIEW_STORAGE_KEY, type CleanupPreviewOptions } from './CleanupPreview'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'
import { DEFAULT_CLEANUP_SETTINGS } from './PipelineControls'
import { LIVE_PREVIEW_STORAGE_KEY, PREVIEW_DEBOUNCE_MS } from './TracePreview'

const layers: WorkspaceLayer[] = [
  { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
]

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

function workspace(cleanupPreview?: CleanupPreviewOptions) {
  return <ImageWorkspace layers={layers} editorMode canvasLabel="Cleanup canvas" cleanupPreview={cleanupPreview} />
}

// Renders a 1000 x 700 image in an 800 x 600 viewport, fitted (scale 0.768).
function prepare(cleanupPreview?: CleanupPreviewOptions) {
  const result = render(workspace(cleanupPreview))
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

const SIZED: CleanupSettings = { ...DEFAULT_CLEANUP_SETTINGS, width_mm: 300, cleanup: true, min_region_area: 2 }
const options = (overrides: Partial<CleanupPreviewOptions> = {}): CleanupPreviewOptions => ({
  revisionId: 'rev-1', settings: SIZED, tracedSize: { width: 1000, height: 700 }, ...overrides,
})
const WINDOW = { x: 166, y: 100, width: 668, height: 500 }
const PIXELS = 'data:image/png;base64,cGl4ZWxz'
const CHANGES = 'data:image/png;base64,Y2hhbmdlcw=='

function zoomTwice() {
  // Two 1.25x zooms about the centre: scale 1.2, origin (-200, -120).
  fireEvent.click(screen.getByLabelText('Zoom in'))
  fireEvent.click(screen.getByLabelText('Zoom in'))
}

function requestBody(entry: Pending) {
  return JSON.parse(String(entry.init.body)) as { window: typeof WINDOW; cleanup: CleanupSettings }
}

async function respond(entry: Pending, body: unknown, ok = true) {
  await act(async () => {
    entry.resolve({ ok, statusText: ok ? 'OK' : 'Unprocessable Entity', json: async () => body, text: async () => JSON.stringify(body) })
  })
}

const ok = (window = WINDOW, pixels = PIXELS) => ({ window, crop: window, image: { width: 1000, height: 700 }, pixels, changes: CHANGES, removed_pixels: 3, widened_pixels: 0, elapsed_ms: 4 })

describe('Cleanup canvas live preview', () => {
  it('shows its switch only on a canvas given cleanup-preview options', () => {
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
    expect(screen.getByRole('status').textContent).toBe('Zoom in to preview')
    expect(pending).toHaveLength(0)
  })

  it('cleans the zoomed-in window with the unsaved form and draws the pixels and changes on it', async () => {
    const { container } = prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS - 1) })
    expect(pending).toHaveLength(0)
    act(() => { vi.advanceTimersByTime(1) })
    expect(pending).toHaveLength(1)
    expect(pending[0].url).toBe('/api/revisions/rev-1/cleanup-preview')
    expect(pending[0].init.method).toBe('POST')
    // The form's values as they stand, physical size included.
    expect(requestBody(pending[0])).toEqual({ window: WINDOW, cleanup: SIZED })
    expect(screen.getByText('Updating live preview…')).toBeTruthy()
    await respond(pending[0], ok())
    const pixels = screen.getByAltText('Live cleanup preview') as HTMLImageElement
    const changes = screen.getByAltText(/Cleanup changes/) as HTMLImageElement
    // Inside the transformed stage, on the same window, so both register exactly.
    for (const image of [pixels, changes]) {
      expect(image.parentElement?.classList.contains('image-stage')).toBe(true)
      expect([image.style.left, image.style.top, image.style.width, image.style.height]).toEqual(['166px', '100px', '668px', '500px'])
    }
    expect(pixels.src).toBe(PIXELS)
    expect(changes.src).toBe(CHANGES)
    // The changes are drawn on top of the cleaned pixels.
    expect(pixels.compareDocumentPosition(changes) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(container.querySelector('.trace-preview-badge:not(.is-busy)')?.textContent).toBe('Live preview')
  })

  it('places a larger raster grid window on the displayed image scale', async () => {
    prepare(options({ tracedSize: { width: 2000, height: 1400 } }))
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    for (let step = 0; step < 3; step++) fireEvent.click(screen.getByLabelText('Zoom in'))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    const window = requestBody(pending[0]).window
    expect(window.width * window.height).toBeLessThanOrEqual(600 * 600)
    await respond(pending[0], ok(window))
    const pixels = screen.getByAltText('Live cleanup preview') as HTMLImageElement
    expect(parseFloat(pixels.style.left)).toBeCloseTo(window.x / 2)
    expect(parseFloat(pixels.style.width)).toBeCloseTo(window.width / 2)
  })

  it('toggles the changes overlay without re-requesting and remembers the choice', async () => {
    prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(pending[0], ok())
    // A switch, marked up like the Live preview switch.
    const toggle = screen.getByRole('switch', { name: 'Show changes' }) as HTMLInputElement
    expect(toggle.checked).toBe(true)
    fireEvent.click(toggle)
    expect(screen.queryByAltText(/Cleanup changes/)).toBeNull()
    expect(screen.getByAltText('Live cleanup preview')).toBeTruthy()
    expect(localStorage.getItem(CLEANUP_CHANGES_STORAGE_KEY)).toBe('off')
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS * 2) })
    expect(pending).toHaveLength(1)
    cleanup()
    prepare(options())
    expect((screen.getByRole('switch', { name: 'Show changes' }) as HTMLInputElement).checked).toBe(false)
    fireEvent.click(screen.getByRole('switch', { name: 'Show changes' }))
    expect(localStorage.getItem(CLEANUP_CHANGES_STORAGE_KEY)).toBe('on')
  })

  it('debounces setting changes, aborts the request in flight and ignores its late response', async () => {
    const { rerender } = prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    const first = pending[0]
    rerender(workspace(options({ settings: { ...SIZED, width_mm: 250 } })))
    expect(first.init.signal?.aborted).toBe(true)
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS / 2) })
    rerender(workspace(options({ settings: { ...SIZED, width_mm: 200 } })))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS - 1) })
    expect(pending).toHaveLength(1)
    act(() => { vi.advanceTimersByTime(1) })
    // The intermediate 250 mm never reached the server.
    expect(pending).toHaveLength(2)
    expect(requestBody(pending[1]).cleanup.width_mm).toBe(200)
    await respond(first, ok(WINDOW, 'data:image/png;base64,c3RhbGU='))
    expect(screen.queryByAltText('Live cleanup preview')).toBeNull()
    await respond(pending[1], ok(WINDOW, 'data:image/png;base64,ZnJlc2g='))
    expect((screen.getByAltText('Live cleanup preview') as HTMLImageElement).src).toBe('data:image/png;base64,ZnJlc2g=')
  })

  it('reports invalid settings and server errors, hiding the last preview under them', async () => {
    const { rerender } = prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(pending[0], ok())
    rerender(workspace(options({ settings: { ...SIZED, min_region_area: -1 } })))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(pending[1], { detail: [{ loc: ['body', 'cleanup', 'min_region_area'], msg: 'bad' }] }, false)
    expect(screen.getByRole('status').textContent).toBe('Preview failed: The cleanup settings are not valid for a preview')
    expect(screen.queryByAltText('Live cleanup preview')).toBeNull()
    rerender(workspace(options({ settings: { ...SIZED, remove_colour: 'Nonexistent' } })))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    expect(screen.getByRole('status').textContent).toBe('')
    await respond(pending[2], { detail: "unknown colour 'Nonexistent'" }, false)
    expect(screen.getByRole('status').textContent).toBe("Preview failed: unknown colour 'Nonexistent'")
  })

  it('remembers its switch under its own key, apart from the trace switch', () => {
    localStorage.setItem(LIVE_PREVIEW_STORAGE_KEY, 'on')
    prepare(options())
    const toggle = screen.getByRole('switch', { name: 'Live preview' }) as HTMLInputElement
    expect(toggle.checked).toBe(false)
    fireEvent.click(toggle)
    expect(localStorage.getItem(CLEANUP_PREVIEW_STORAGE_KEY)).toBe('on')
    cleanup()
    prepare(options())
    expect((screen.getByRole('switch', { name: 'Live preview' }) as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    expect(localStorage.getItem(CLEANUP_PREVIEW_STORAGE_KEY)).toBe('off')
    expect(localStorage.getItem(LIVE_PREVIEW_STORAGE_KEY)).toBe('on')
  })

  it('requests nothing while paused and says why', () => {
    render(<ImageWorkspace layers={layers} editorMode canvasLabel="Cleanup canvas" cleanupPreview={{ ...options(), tracedSize: null }} livePreviewPaused livePreviewStatus="Preview unavailable: the selected revision failed to load." />)
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS * 4) })
    expect(pending).toHaveLength(0)
    expect(screen.getByText('Preview unavailable: the selected revision failed to load.')).toBeTruthy()
  })
})
