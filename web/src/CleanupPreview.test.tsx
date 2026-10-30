// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CleanupSettings } from './api'
import { CLEANUP_CHANGES_OVERLAY_KIND, CLEANUP_CHANGES_STORAGE_KEY, CLEANUP_PREVIEW_STORAGE_KEY, MEASURING_BADGE_TEXT, type CleanupPreviewOptions } from './CleanupPreview'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'
import { DEFAULT_CLEANUP_SETTINGS } from './PipelineControls'
import { BUSY_NOTE_POLL_MS, PREVIEW_DEBOUNCE_MS } from './LivePreview'

const layers: WorkspaceLayer[] = [
  { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
]

// The shown revision's changes overlay, as App registers it.
const APPLIED: WorkspaceLayer = { id: 'changes', name: 'Cleanup changes (removed red, widened green)', url: '/changes.png', mediaType: 'image/png', role: 'overlay', kind: CLEANUP_CHANGES_OVERLAY_KIND }

function loadUrl(container: HTMLElement, suffix: string, width: number, height: number) {
  for (const image of [...container.querySelectorAll('img')].filter(node => node.src.endsWith(suffix)) as HTMLImageElement[]) {
    Object.defineProperties(image, { naturalWidth: { configurable: true, value: width }, naturalHeight: { configurable: true, value: height } })
    fireEvent.load(image)
  }
}

type Pending = { url: string; init: RequestInit; resolve: (response: unknown) => void }
// Preview requests; the reads of the regions state go to `stateReads`.
let pending: Pending[] = []
let stateReads: Pending[] = []

beforeEach(() => {
  pending = []
  stateReads = []
  localStorage.clear()
  vi.useFakeTimers()
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => new Promise(resolve =>
    (url.endsWith('/cleanup-preview/regions') ? stateReads : pending).push({ url, init, resolve }))))
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
    const changes = screen.getByAltText(/What Cleanup would change/) as HTMLImageElement
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

  it('always draws what would change with Live preview on, whatever Show changes says', async () => {
    prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(pending[0], ok())
    expect(screen.getByAltText(/What Cleanup would change/)).toBeTruthy()
    localStorage.setItem(CLEANUP_CHANGES_STORAGE_KEY, 'off')
    cleanup()
    prepare(options())
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(pending[1], ok())
    expect((screen.getByRole('switch', { name: 'Show changes' }) as HTMLInputElement).checked).toBe(false)
    expect(screen.getByAltText(/What Cleanup would change/)).toBeTruthy()
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
    expect(screen.getByRole('status').textContent).toBe('Updating live preview…')
    await respond(pending[2], { detail: "unknown colour 'Nonexistent'" }, false)
    expect(screen.getByRole('status').textContent).toBe("Preview failed: unknown colour 'Nonexistent'")
  })

  it('remembers its switch under its own key', () => {
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
  })

  it('Show changes draws the cleanup run\'s changes over the whole image, with Live preview off, and is remembered', () => {
    const withOverlay: WorkspaceLayer[] = [...layers, APPLIED]
    const { container } = render(<ImageWorkspace layers={withOverlay} editorMode canvasLabel="Cleanup canvas" inspectionKinds={[CLEANUP_CHANGES_OVERLAY_KIND]} cleanupPreview={options()} />)
    loadUrl(container, '/flat.png', 1000, 700)
    loadUrl(container, '/changes.png', 1000, 700)
    expect((screen.getByRole('switch', { name: 'Live preview' }) as HTMLInputElement).checked).toBe(false)
    const toggle = screen.getByRole('switch', { name: 'Show changes' }) as HTMLInputElement
    expect(toggle.disabled).toBe(false)
    // On by default: the whole-image overlay, independent of any preview.
    expect(toggle.checked).toBe(true)
    const overlay = screen.getByAltText(APPLIED.name) as HTMLImageElement
    expect(overlay.classList.contains('inspection-overlay')).toBe(true)
    expect(overlay.getAttribute('src')).toBe('/changes.png')
    // The switch is its only control: no second toggle among the inspection layers.
    expect(screen.queryByRole('group', { name: 'Inspection layers' })).toBeNull()
    fireEvent.click(toggle)
    expect(screen.queryByAltText(APPLIED.name)).toBeNull()
    expect(localStorage.getItem(CLEANUP_CHANGES_STORAGE_KEY)).toBe('off')
    expect(pending).toHaveLength(0)
    cleanup()
    const again = render(<ImageWorkspace layers={withOverlay} editorMode canvasLabel="Cleanup canvas" inspectionKinds={[CLEANUP_CHANGES_OVERLAY_KIND]} cleanupPreview={options()} />)
    loadUrl(again.container, '/flat.png', 1000, 700)
    loadUrl(again.container, '/changes.png', 1000, 700)
    expect((screen.getByRole('switch', { name: 'Show changes' }) as HTMLInputElement).checked).toBe(false)
    expect(screen.queryByAltText(APPLIED.name)).toBeNull()
    fireEvent.click(screen.getByRole('switch', { name: 'Show changes' }))
    expect(localStorage.getItem(CLEANUP_CHANGES_STORAGE_KEY)).toBe('on')
    expect(screen.getByAltText(APPLIED.name)).toBeTruthy()
  })

  it('disables Show changes with the reason when the revision has no changes overlay', () => {
    prepare(options({ changesUnavailable: 'Re-run Cleanup to record its changes' }))
    const toggle = screen.getByRole('switch', { name: 'Show changes' }) as HTMLInputElement
    expect(toggle.disabled).toBe(true)
    expect(toggle.checked).toBe(false)
    const reason = document.getElementById(toggle.getAttribute('aria-describedby') ?? '')
    expect(reason?.textContent).toBe('Re-run Cleanup to record its changes')
  })

  it('leaves the changes overlay to its own toggle on a canvas without the cleanup preview', () => {
    const withOverlay: WorkspaceLayer[] = [...layers, APPLIED]
    const { container } = render(<ImageWorkspace layers={withOverlay} editorMode canvasLabel="Trace canvas" inspectionKinds={[CLEANUP_CHANGES_OVERLAY_KIND]} />)
    loadUrl(container, '/flat.png', 1000, 700)
    expect(screen.queryByRole('switch', { name: 'Show changes' })).toBeNull()
    expect(within(screen.getByRole('group', { name: 'Inspection layers' })).getByLabelText(APPLIED.name)).toBeTruthy()
  })

  it('labels the colours of each overlay on view, and only those', async () => {
    const withOverlay: WorkspaceLayer[] = [...layers, APPLIED]
    const LIVE = 'Live: red would be removed, green widened, blue restored'
    const APPLIED_LINE = 'Show changes: red removed, green widened by the last Cleanup'
    const legend = () => screen.queryByRole('list', { name: 'Overlay colours' })?.textContent ?? null
    const { container } = render(<ImageWorkspace layers={withOverlay} editorMode canvasLabel="Cleanup canvas" inspectionKinds={[CLEANUP_CHANGES_OVERLAY_KIND]} cleanupPreview={options()} />)
    const viewport = within(container).getByLabelText('Image comparison viewport')
    Object.defineProperties(viewport, { clientWidth: { configurable: true, value: 800 }, clientHeight: { configurable: true, value: 600 } })
    loadUrl(container, '/flat.png', 1000, 700)
    loadUrl(container, '/changes.png', 1000, 700)
    // Show changes alone.
    expect(legend()).toBe(APPLIED_LINE)
    // Both.
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(pending[0], ok())
    expect(legend()).toBe(LIVE + APPLIED_LINE)
    // The live preview alone.
    fireEvent.click(screen.getByRole('switch', { name: 'Show changes' }))
    expect(legend()).toBe(LIVE)
    // Neither.
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    expect(legend()).toBeNull()
  })

  it('ignores the switch state saved when it meant the preview highlight', () => {
    localStorage.setItem('vinylgen.cleanupPreviewChanges', 'off')
    const { container } = render(<ImageWorkspace layers={[...layers, APPLIED]} editorMode canvasLabel="Cleanup canvas" inspectionKinds={[CLEANUP_CHANGES_OVERLAY_KIND]} cleanupPreview={options()} />)
    loadUrl(container, '/flat.png', 1000, 700)
    loadUrl(container, '/changes.png', 1000, 700)
    expect((screen.getByRole('switch', { name: 'Show changes' }) as HTMLInputElement).checked).toBe(true)
    expect(screen.getByAltText(APPLIED.name)).toBeTruthy()
  })

  it('keeps Show changes operable while the live preview is paused', () => {
    const { container } = render(<ImageWorkspace layers={[...layers, APPLIED]} editorMode canvasLabel="Cleanup canvas" inspectionKinds={[CLEANUP_CHANGES_OVERLAY_KIND]} cleanupPreview={{ ...options(), tracedSize: null }} livePreviewPaused livePreviewStatus="Preview unavailable: the selected revision failed to load." />)
    loadUrl(container, '/flat.png', 1000, 700)
    loadUrl(container, '/changes.png', 1000, 700)
    const toggle = screen.getByRole('switch', { name: 'Show changes' }) as HTMLInputElement
    expect(toggle.disabled).toBe(false)
    expect(screen.getByAltText(APPLIED.name)).toBeTruthy()
    fireEvent.click(toggle)
    expect(screen.queryByAltText(APPLIED.name)).toBeNull()
    expect(screen.getByText('Preview unavailable: the selected revision failed to load.')).toBeTruthy()
  })

  it('says it is measuring regions only while the server reports measuring the revision', async () => {
    const { container, rerender } = prepare(options())
    const badge = () => container.querySelector('.trace-preview-badge')?.textContent
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    // The state is read as the request starts, without waiting for it.
    expect(pending).toHaveLength(1)
    expect(stateReads).toHaveLength(1)
    expect(stateReads[0].url).toBe('/api/revisions/rev-1/cleanup-preview/regions')
    expect(stateReads[0].init.method ?? 'GET').toBe('GET')
    expect(badge()).toBe('Updating live preview…')
    await respond(stateReads[0], { measured: false, measuring: false })
    expect(badge()).toBe('Updating live preview…')
    // Re-read while the request is in flight: the server has started measuring.
    expect(stateReads).toHaveLength(1)
    act(() => { vi.advanceTimersByTime(BUSY_NOTE_POLL_MS) })
    expect(stateReads).toHaveLength(2)
    await respond(stateReads[1], { measured: false, measuring: true })
    expect(badge()).toBe(MEASURING_BADGE_TEXT)
    // A polite live region announces it; nothing hides it from assistive technology.
    const status = screen.getByRole('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toBe(MEASURING_BADGE_TEXT)
    expect(status.querySelector('[aria-hidden="true"]')?.tagName.toLowerCase()).toBe('svg')
    expect(container.querySelector('.trace-preview-badge')?.closest('[aria-hidden="true"]')).toBeNull()
    // Measured: the window's own clean continues under the ordinary badge.
    act(() => { vi.advanceTimersByTime(BUSY_NOTE_POLL_MS) })
    await respond(stateReads[2], { measured: true, measuring: false })
    expect(badge()).toBe('Updating live preview…')
    await respond(pending[0], ok())
    expect(badge()).toBe('Live preview')
    // Idle: the label stays visible but is not announced, unlike the states above.
    expect(status.textContent).toBe('')
    expect(container.querySelector('.trace-preview-badge')?.getAttribute('aria-hidden')).toBe('true')
    // Settled: no more reads.
    act(() => { vi.advanceTimersByTime(BUSY_NOTE_POLL_MS * 4) })
    expect(stateReads).toHaveLength(3)
    // A superseded request keeps the note until the next request's first read, since
    // the server may still be measuring for it.
    rerender(workspace(options({ settings: { ...SIZED, min_region_area: 5 } })))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(stateReads[3], { measured: false, measuring: true })
    expect(badge()).toBe(MEASURING_BADGE_TEXT)
    rerender(workspace(options({ settings: { ...SIZED, min_region_area: 6 } })))
    expect(pending[1].init.signal?.aborted).toBe(true)
    expect(stateReads[3].init.signal?.aborted).toBe(true)
    expect(badge()).toBe(MEASURING_BADGE_TEXT)
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(stateReads[4], { measured: true, measuring: false })
    expect(badge()).toBe('Updating live preview…')
    // A note read for another revision is never shown for this one.
    act(() => { vi.advanceTimersByTime(BUSY_NOTE_POLL_MS) })
    await respond(stateReads[5], { measured: false, measuring: true })
    expect(badge()).toBe(MEASURING_BADGE_TEXT)
    rerender(workspace(options({ revisionId: 'rev-2' })))
    expect(badge()).toBe('Updating live preview…')
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    expect(stateReads[6].url).toBe('/api/revisions/rev-2/cleanup-preview/regions')
  })

  it('keeps the ordinary badge when the regions state cannot be read', async () => {
    const { container } = prepare(options())
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    zoomTwice()
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS) })
    await respond(stateReads[0], { detail: 'Resource not found: rev-1' }, false)
    expect(container.querySelector('.trace-preview-badge')?.textContent).toBe('Updating live preview…')
    expect(screen.getByRole('status').textContent).toBe('Updating live preview…')
    // It is read again while the request is in flight.
    act(() => { vi.advanceTimersByTime(BUSY_NOTE_POLL_MS) })
    expect(stateReads).toHaveLength(2)
  })

  it('requests nothing while paused and says why', () => {
    render(<ImageWorkspace layers={layers} editorMode canvasLabel="Cleanup canvas" cleanupPreview={{ ...options(), tracedSize: null }} livePreviewPaused livePreviewStatus="Preview unavailable: the selected revision failed to load." />)
    fireEvent.click(screen.getByRole('switch', { name: 'Live preview' }))
    act(() => { vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS * 4) })
    expect(pending).toHaveLength(0)
    expect(screen.getByText('Preview unavailable: the selected revision failed to load.')).toBeTruthy()
  })
})
