import { type KeyboardEvent as ReactKeyboardEvent, PointerEvent, WheelEvent, useEffect, useId, useRef, useState } from 'react'
import { LoaderCircle, Maximize, ZoomIn, ZoomOut } from 'lucide-react'
import { clamp, defaultComparePair, distance, fitTransform, midpoint, pixelLine, sameImageSize, splitPositionInImage, zoomAt, type ImageSize, type Point, type ViewTransform } from './workspace'
import type { OverlayFrame } from './regionSelection'
import { useTracePreview, type LivePreviewOptions } from './TracePreview'

// `sizeFrom` names the raster whose pixel grid a vector layer is drawn on: an SVG
// sized in physical units reports an intrinsic size in CSS pixels, not the pixel
// grid its viewBox (and every registered raster) uses.
// `kind` names what a layer is independently of its artifact id, so an inspection toggle
// keyed by it stays on when a new run's layer of the same kind replaces the old one.
export type WorkspaceLayer = { id: string; name: string; url: string; mediaType: string; role: 'image' | 'mask' | 'overlay'; sizeFrom?: string; kind?: string }
const inspectionKey = (layer: WorkspaceLayer) => layer.kind ?? layer.id
// The absolute URL an <img> resolves a layer's (possibly relative) URL to.
const absoluteUrl = (url: string) => new URL(url, document.baseURI).href
type CompareMode = 'single' | 'split' | 'blink'

export function ImageWorkspace({ layers, preferredLayerId, selectionLabel, onSelectPoint, paintMode = false, onPaint, overlay, editorMode = false, canvasLabel = 'Palette canvas', inspectionToggles = false, loading = false, livePreview, onClose }: { layers: WorkspaceLayer[]; preferredLayerId?: string; selectionLabel?: string; onSelectPoint?: (point: [number, number]) => void; paintMode?: boolean; onPaint?: (x: number, y: number) => void; overlay?: OverlayFrame; editorMode?: boolean; canvasLabel?: string; inspectionToggles?: boolean; loading?: boolean; livePreview?: LivePreviewOptions; onClose?: () => void }) {
  const viewport = useRef<HTMLDivElement>(null)
  const overlayCanvas = useRef<HTMLCanvasElement>(null)
  const overlayPatch = useRef<{ canvas: HTMLCanvasElement; bounds: OverlayFrame['bounds'] } | null>(null)
  const pointers = useRef(new Map<number, Point>())
  const pointerStarts = useRef(new Map<number, Point>())
  const gesture = useRef<{ distance: number; centre: Point; transform: ViewTransform } | null>(null)
  const painting = useRef(false)
  const dividerDragging = useRef(false)
  const lastPaint = useRef<[number, number] | null>(null)
  const panningPointer = useRef(false)
  const spaceHeld = useRef(false)
  const [spacePan, setSpacePan] = useState(false)
  const [panDragging, setPanDragging] = useState(false)
  const [hoverPixel, setHoverPixel] = useState<[number, number] | null>(null)
  const [transform, setTransform] = useState<ViewTransform>({ x: 0, y: 0, scale: 1 })
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [beforeId, setBeforeId] = useState(() => defaultComparePair(layers.filter(layer => layer.role === 'image').map(layer => layer.id), preferredLayerId).beforeId)
  const [afterId, setAfterId] = useState(() => defaultComparePair(layers.filter(layer => layer.role === 'image').map(layer => layer.id), preferredLayerId).afterId)
  const previousPreferredLayerId = useRef(preferredLayerId)
  const [mode, setMode] = useState<CompareMode>('single')
  const [split, setSplit] = useState(50)
  const [blinkAfter, setBlinkAfter] = useState(false)
  const [reducedMotion, setReducedMotion] = useState(() => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  const [blinkPaused, setBlinkPaused] = useState(() => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  const [sheetOpen, setSheetOpen] = useState(false)
  const sheetTitleId = useId()
  const sheetRoot = useRef<HTMLDivElement>(null)
  const sheetPanel = useRef<HTMLDivElement>(null)
  const controlsButton = useRef<HTMLButtonElement>(null)
  const doneButton = useRef<HTMLButtonElement>(null)
  const [naturalDimensions, setDimensions] = useState<Record<string, ImageSize>>({})
  const dimensions: Record<string, ImageSize> = { ...naturalDimensions }
  for (const layer of layers) {
    const adopted = layer.sizeFrom ? naturalDimensions[layer.sizeFrom] : undefined
    if (adopted && naturalDimensions[layer.id]) dimensions[layer.id] = adopted
  }
  const [enabledInspections, setEnabledInspections] = useState<Record<string, boolean>>({})
  // The absolute URL whose Before <img> request has settled (loaded or failed); any other
  // shown URL is still loading.
  const [loadedUrl, setLoadedUrl] = useState<string | null>(null)

  const visible = layers.filter(layer => layer.role === 'image')
  // When the chosen Before layer disappears (a new revision's layers arrive before the
  // effects below re-point it), fall back to the preferred layer in this same render so
  // no other layer is ever shown or fetched in between.
  const before = visible.find(layer => layer.id === beforeId)
    ?? visible.find(layer => layer.id === preferredLayerId) ?? visible[0]
  const imageLoading = Boolean(before) && loadedUrl !== absoluteUrl(before!.url)
  const busy = loading || imageLoading
  // Filled after mount so a live region that mounts already busy still announces it.
  const [announcement, setAnnouncement] = useState('')
  useEffect(() => { setAnnouncement(busy ? 'Loading image…' : '') }, [busy])
  const after = visible.find(layer => layer.id === afterId)
  const inspections = layers.filter(layer => layer.role === 'mask' || layer.role === 'overlay')
  const beforeSize = before ? dimensions[before.id] : undefined
  const compatibleAfter = after && before && after.id !== before.id && beforeSize && sameImageSize(beforeSize, dimensions[after.id]) ? after : undefined
  const comparableImages = before ? visible.filter(layer => layer.id !== before.id && beforeSize && sameImageSize(beforeSize, dimensions[layer.id])) : []
  const comparisonAvailable = comparableImages.length > 0
  const compatibleInspections = inspections.filter(layer => enabledInspections[inspectionKey(layer)] && beforeSize && sameImageSize(beforeSize, dimensions[layer.id]))
  const incompatibleInspections = inspections.filter(layer => enabledInspections[inspectionKey(layer)] && beforeSize && dimensions[layer.id] && !sameImageSize(beforeSize, dimensions[layer.id]))

  useEffect(() => {
    const ids = visible.map(layer => layer.id)
    const pair = defaultComparePair(ids, ids.includes(beforeId) ? beforeId : preferredLayerId, dimensions)
    if (pair.beforeId !== beforeId) setBeforeId(pair.beforeId)
    if ((!ids.includes(afterId) || afterId === pair.beforeId) && pair.afterId !== afterId) setAfterId(pair.afterId)
  }, [layers, beforeId, afterId, naturalDimensions, preferredLayerId])

  useEffect(() => {
    // Only force the single-view fallback once availability is genuinely known to
    // be false: Before's intrinsic dimensions have loaded and no distinct
    // compatible pair exists. While Before is still measuring, comparison
    // availability is unknown, not false, so split/blink must not be dropped.
    if (beforeSize && (mode === 'split' || mode === 'blink') && !comparisonAvailable) setMode('single')
  }, [comparisonAvailable, mode, beforeSize])

  useEffect(() => {
    if (preferredLayerId === previousPreferredLayerId.current) return
    previousPreferredLayerId.current = preferredLayerId
    if (preferredLayerId && visible.some(layer => layer.id === preferredLayerId)) {
      setBeforeId(preferredLayerId)
    }
  }, [preferredLayerId, layers])

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    const changed = (event: MediaQueryListEvent) => {
      setReducedMotion(event.matches)
      if (event.matches) setBlinkPaused(true)
    }
    query.addEventListener('change', changed)
    return () => query.removeEventListener('change', changed)
  }, [])

  useEffect(() => {
    if (mode !== 'blink' || blinkPaused) return
    const timer = window.setInterval(() => setBlinkAfter(value => !value), 550)
    return () => window.clearInterval(timer)
  }, [mode, blinkPaused])

  useEffect(() => {
    if (!sheetOpen) return
    const isolated: Array<{ element: HTMLElement; inert: string | null; ariaHidden: string | null }> = []
    let branch: HTMLElement | null = sheetRoot.current
    while (branch?.parentElement) {
      const parent = branch.parentElement
      for (const sibling of [...parent.children]) {
        if (!(sibling instanceof HTMLElement) || sibling === branch) continue
        isolated.push({ element: sibling, inert: sibling.getAttribute('inert'), ariaHidden: sibling.getAttribute('aria-hidden') })
        sibling.setAttribute('inert', '')
        sibling.setAttribute('aria-hidden', 'true')
      }
      if (parent === document.body) break
      branch = parent
    }
    doneButton.current?.focus()
    const keyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        setSheetOpen(false)
        return
      }
      if (event.key !== 'Tab' || !sheetPanel.current) return
      const focusable = [...sheetPanel.current.querySelectorAll<HTMLElement>('button:not(:disabled), select:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])')]
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (!first || !last) return
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    const containFocus = (event: FocusEvent) => {
      if (!sheetPanel.current?.contains(event.target as Node)) doneButton.current?.focus()
    }
    document.addEventListener('keydown', keyDown)
    document.addEventListener('focusin', containFocus)
    return () => {
      document.removeEventListener('keydown', keyDown)
      document.removeEventListener('focusin', containFocus)
      for (const { element, inert, ariaHidden } of isolated) {
        if (inert === null) element.removeAttribute('inert')
        else element.setAttribute('inert', inert)
        if (ariaHidden === null) element.removeAttribute('aria-hidden')
        else element.setAttribute('aria-hidden', ariaHidden)
      }
      controlsButton.current?.focus()
    }
  }, [sheetOpen])

  useEffect(() => {
    if (!paintMode) return
    const down = (event: KeyboardEvent) => {
      if (event.code !== 'Space' || event.repeat) return
      const target = event.target as HTMLElement | null
      if (target && (target.tagName === 'INPUT' || target.tagName === 'SELECT' || target.tagName === 'TEXTAREA' || target.isContentEditable || target.closest('[aria-modal="true"]'))) return
      event.preventDefault()
      spaceHeld.current = true
      setSpacePan(true)
    }
    const up = (event: KeyboardEvent) => {
      if (event.code !== 'Space') return
      spaceHeld.current = false
      setSpacePan(false)
    }
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    return () => {
      window.removeEventListener('keydown', down)
      window.removeEventListener('keyup', up)
      spaceHeld.current = false
      setSpacePan(false)
    }
  }, [paintMode])

  useEffect(() => {
    // Cache the overlay's bounding-box pixels on a small offscreen canvas so the
    // draw pass can blit it without rebuilding on every pan/zoom frame.
    if (!overlay) { overlayPatch.current = null; return }
    const { bounds, data } = overlay
    const patch = document.createElement('canvas')
    patch.width = bounds.width; patch.height = bounds.height
    const context = patch.getContext('2d')
    if (!context) { overlayPatch.current = null; return }
    const image = context.createImageData(bounds.width, bounds.height)
    image.data.set(data)
    context.putImageData(image, 0, 0)
    overlayPatch.current = { canvas: patch, bounds }
  }, [overlay])

  useEffect(() => {
    // Draw the overlay in screen space: a viewport-sized, device-resolution canvas
    // that is NOT itself CSS-transformed. We apply the pan/zoom via the 2d context and
    // upscale the patch with nearest-neighbour ourselves. Firefox (and WebKit) blur and
    // sub-pixel-shift a CSS-`transform: scale`d <canvas> backing store — ignoring
    // image-rendering: pixelated — so drawing an image-sized canvas inside the
    // transformed stage misaligned the overlay from the base image at zoom. Rendering at
    // device pixels here keeps it crisp and pixel-aligned across browsers, and avoids the
    // max-canvas-size limit a gigapixel backing store would hit. jsdom has no 2d context,
    // so this is a no-op under test.
    const canvas = overlayCanvas.current
    if (!canvas) return
    const dpr = window.devicePixelRatio || 1
    const backingWidth = Math.max(1, Math.round(canvas.clientWidth * dpr))
    const backingHeight = Math.max(1, Math.round(canvas.clientHeight * dpr))
    if (canvas.width !== backingWidth || canvas.height !== backingHeight) {
      canvas.width = backingWidth; canvas.height = backingHeight
    }
    const context = canvas.getContext('2d')
    if (!context) return
    context.setTransform(1, 0, 0, 1, 0, 0)
    context.clearRect(0, 0, canvas.width, canvas.height)
    const patch = overlayPatch.current
    if (!patch) return
    // Map image pixels to device pixels exactly as the base image is placed: an image
    // pixel (ix, iy) lands at viewport CSS (transform.x + ix·scale, transform.y + iy·scale).
    context.setTransform(dpr * transform.scale, 0, 0, dpr * transform.scale,
                         dpr * transform.x, dpr * transform.y)
    context.imageSmoothingEnabled = false
    context.drawImage(patch.canvas, patch.bounds.x, patch.bounds.y)
  }, [overlay, transform])

  useEffect(() => {
    const element = viewport.current
    if (!element || !size.width || !size.height || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(entries => {
      const bounds = entries[0]?.contentRect
      if (bounds?.width && bounds.height) {
        setTransform(fitTransform(bounds.width, bounds.height, size.width, size.height))
      }
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [size.width, size.height])

  function fit(width = size.width, height = size.height) {
    setTransform(fitTransform(viewport.current?.clientWidth ?? 0, viewport.current?.clientHeight ?? 0, width, height))
  }

  function zoomCentre(factor: number) {
    setTransform(value => zoomAt(value, { x: (viewport.current?.clientWidth ?? 0) / 2, y: (viewport.current?.clientHeight ?? 0) / 2 }, factor))
  }

  // Record which URL the Before <img> settled on (the one it actually holds), and take
  // its natural size only when that is the shown layer's own image.
  function beforeSettled(event: React.SyntheticEvent<HTMLImageElement>, loaded: boolean) {
    const settled = event.currentTarget.currentSrc || event.currentTarget.src
    setLoadedUrl(settled)
    if (loaded && before && settled === absoluteUrl(before.url)) imageLoaded(before.id, event)
  }

  function imageLoaded(layerId: string, event: React.SyntheticEvent<HTMLImageElement>) {
    const image = event.currentTarget
    if (image.naturalWidth) setDimensions(value => value[layerId]?.width === image.naturalWidth && value[layerId]?.height === image.naturalHeight
      ? value : ({ ...value, [layerId]: { width: image.naturalWidth, height: image.naturalHeight } }))
  }

  useEffect(() => {
    // Size the stage to Before's registered pixel grid once it is known, refitting only
    // when that grid changes so toggling between same-size views keeps the zoom.
    if (!beforeSize || (size.width === beforeSize.width && size.height === beforeSize.height)) return
    setSize(beforeSize)
    fit(beforeSize.width, beforeSize.height)
  }, [before?.id, beforeSize?.width, beforeSize?.height, size.width, size.height])

  function localPoint(clientX: number, clientY: number) {
    const bounds = viewport.current!.getBoundingClientRect()
    return { x: clientX - bounds.left, y: clientY - bounds.top }
  }

  function imagePixel(clientX: number, clientY: number): [number, number] | null {
    if (!size.width || !size.height) return null
    const here = localPoint(clientX, clientY)
    const x = Math.floor((here.x - transform.x) / transform.scale)
    const y = Math.floor((here.y - transform.y) / transform.scale)
    return x >= 0 && y >= 0 && x < size.width && y < size.height ? [x, y] : null
  }

  function pointerDown(event: PointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId)
    const here = localPoint(event.clientX, event.clientY)
    pointers.current.set(event.pointerId, here)
    pointerStarts.current.set(event.pointerId, here)
    if (pointers.current.size === 2) {
      // A second finger begins a pinch; abandon any stroke so it does not paint
      // a line to the zoom's focus.
      painting.current = false
      lastPaint.current = null
      panningPointer.current = false
      setPanDragging(false)
      const [a, b] = [...pointers.current.values()]
      gesture.current = { distance: distance(a, b), centre: midpoint(a, b), transform }
    } else if (paintMode && (event.button === 1 || spaceHeld.current)) {
      // Middle-button or Space-held drag pans the canvas instead of painting.
      if (event.button === 1) event.preventDefault()
      painting.current = false
      lastPaint.current = null
      panningPointer.current = true
      setPanDragging(true)
      setHoverPixel(null)
    } else if (paintMode && onPaint) {
      const pixel = imagePixel(event.clientX, event.clientY)
      painting.current = true
      lastPaint.current = pixel
      if (pixel) onPaint(pixel[0], pixel[1])
    }
  }

  function pointerMove(event: PointerEvent<HTMLDivElement>) {
    if (paintMode) setHoverPixel(panningPointer.current || spaceHeld.current ? null : imagePixel(event.clientX, event.clientY))
    const previous = pointers.current.get(event.pointerId)
    if (!previous) return
    const current = localPoint(event.clientX, event.clientY)
    pointers.current.set(event.pointerId, current)
    if (pointers.current.size === 2 && gesture.current) {
      const [a, b] = [...pointers.current.values()]
      const centre = midpoint(a, b)
      const zoomed = zoomAt(gesture.current.transform, gesture.current.centre, distance(a, b) / gesture.current.distance)
      setTransform({ ...zoomed, x: zoomed.x + centre.x - gesture.current.centre.x, y: zoomed.y + centre.y - gesture.current.centre.y })
    } else if (panningPointer.current) {
      setTransform(value => ({ ...value, x: value.x + current.x - previous.x, y: value.y + current.y - previous.y }))
    } else if (paintMode && onPaint) {
      if (!painting.current) return
      const pixel = imagePixel(event.clientX, event.clientY)
      if (!pixel) return
      const [last] = [lastPaint.current]
      for (const [x, y] of pixelLine(last?.[0] ?? null, last?.[1] ?? null, pixel[0], pixel[1])) {
        if (x >= 0 && y >= 0 && x < size.width && y < size.height) onPaint(x, y)
      }
      lastPaint.current = pixel
    } else if (pointers.current.size === 1) {
      setTransform(value => ({ ...value, x: value.x + current.x - previous.x, y: value.y + current.y - previous.y }))
    }
  }

  function pointerUp(event: PointerEvent<HTMLDivElement>) {
    const start = pointerStarts.current.get(event.pointerId)
    const end = localPoint(event.clientX, event.clientY)
    if (onSelectPoint && start && distance(start, end) < 5 && size.width && size.height) {
      const x = Math.floor((end.x - transform.x) / transform.scale)
      const y = Math.floor((end.y - transform.y) / transform.scale)
      if (x >= 0 && y >= 0 && x < size.width && y < size.height) onSelectPoint([x, y])
    }
    painting.current = false
    lastPaint.current = null
    panningPointer.current = false
    setPanDragging(false)
    pointers.current.delete(event.pointerId)
    pointerStarts.current.delete(event.pointerId)
    gesture.current = null
  }

  function wheel(event: WheelEvent<HTMLDivElement>) {
    event.preventDefault()
    setTransform(value => zoomAt(value, localPoint(event.clientX, event.clientY), Math.exp(-event.deltaY * 0.0015)))
  }

  function dividerPointerDown(event: PointerEvent<HTMLDivElement>) {
    event.stopPropagation()
    event.currentTarget.setPointerCapture(event.pointerId)
    dividerDragging.current = true
  }

  function dividerPointerMove(event: PointerEvent<HTMLDivElement>) {
    if (!dividerDragging.current) return
    event.stopPropagation()
    const bounds = viewport.current?.getBoundingClientRect()
    if (!bounds?.width) return
    setSplit(clamp(((event.clientX - bounds.left) / bounds.width) * 100, 0, 100))
  }

  function dividerPointerUp(event: PointerEvent<HTMLDivElement>) {
    if (!dividerDragging.current) return
    event.stopPropagation()
    dividerDragging.current = false
    event.currentTarget.releasePointerCapture?.(event.pointerId)
  }

  function dividerKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    const move = (delta: number) => { event.preventDefault(); setSplit(value => clamp(value + delta, 0, 100)) }
    const set = (position: number) => { event.preventDefault(); setSplit(position) }
    switch (event.key) {
      case 'ArrowLeft': case 'ArrowDown': return move(-1)
      case 'ArrowRight': case 'ArrowUp': return move(1)
      case 'PageDown': return move(-10)
      case 'PageUp': return move(10)
      case 'Home': return set(0)
      case 'End': return set(100)
    }
  }

  const showAfter = mode === 'split' || (mode === 'blink' && blinkAfter)
  const transformStyle = { width: size.width, height: size.height, transform: `translate3d(${transform.x}px, ${transform.y}px, 0) scale(${transform.scale})` }
  // The Trace canvas's live preview of the zoomed-in area (inert without `livePreview`).
  const tracePreview = useTracePreview(livePreview, transform, { width: viewport.current?.clientWidth ?? 0, height: viewport.current?.clientHeight ?? 0 }, size)
  const splitPosition = splitPositionInImage(viewport.current?.clientWidth ?? 0, split, size.width, transform)

  const controls = <>
    {!editorMode && <section className="control-section">
      <h3>Compare</h3>
      <div className="segmented" aria-label="Comparison mode">
        {(['single', 'split', 'blink'] as CompareMode[]).map(value => {
          const locked = value !== 'single' && !comparisonAvailable
          return <button key={value} className={mode === value ? 'active' : ''} aria-pressed={mode === value} disabled={locked} aria-disabled={locked} title={locked ? 'Comparison needs a second image with matching pixel dimensions.' : undefined} onClick={() => { setMode(value); if (value === 'blink') setBlinkAfter(false) }}>{value}</button>
        })}
      </div>
      {!comparisonAvailable && <p className="layer-warning" role="status">Comparison needs a second image with matching pixel dimensions.</p>}
      <label>Before<select value={before?.id ?? ''} onChange={event => { setBeforeId(event.target.value); setSize({ width: 0, height: 0 }) }}>{visible.map(layer => <option key={layer.id} value={layer.id}>{layer.name}</option>)}</select></label>
      <label>After<select value={after?.id ?? ''} onChange={event => setAfterId(event.target.value)}>{visible.map(layer => <option key={layer.id} value={layer.id} disabled={Boolean(beforeSize && dimensions[layer.id] && !sameImageSize(beforeSize, dimensions[layer.id]))}>{layer.name}{beforeSize && dimensions[layer.id] && !sameImageSize(beforeSize, dimensions[layer.id]) ? ' · different canvas' : ''}</option>)}</select></label>
      {after && beforeSize && dimensions[after.id] && !compatibleAfter && <p className="layer-warning" role="status">Comparison hidden: the selected images have different pixel dimensions and cannot be registered exactly.</p>}
      {mode === 'split' && <label>Split · {split}%<input aria-label="Split position" type="range" min="0" max="100" value={split} onChange={event => setSplit(Number(event.target.value))} /></label>}
      {mode === 'blink' && <div className="view-actions">
        <button onClick={() => setBlinkPaused(value => !value)}>{blinkPaused ? 'Resume blink' : 'Pause blink'}</button>
        <button onClick={() => setBlinkAfter(value => !value)}>Show {blinkAfter ? 'before' : 'after'} image</button>
        {reducedMotion && blinkPaused && <p role="status">Blink is paused to respect your reduced-motion preference.</p>}
      </div>}
    </section>}
    {!editorMode && inspections.length > 0 && <section className="control-section inspection-controls">
      <h3>Masks &amp; inspection layers</h3>
      {inspections.map(layer => <label key={layer.id}><input type="checkbox" checked={Boolean(enabledInspections[inspectionKey(layer)])} onChange={event => setEnabledInspections(value => ({ ...value, [inspectionKey(layer)]: event.target.checked }))} />{layer.name}</label>)}
      {incompatibleInspections.length > 0 && <p className="layer-warning" role="status">Hidden because its pixel dimensions differ: {incompatibleInspections.map(layer => layer.name).join(', ')}</p>}
    </section>}
  </>

  if (!visible.length) return <div className="image-workspace empty-workspace"><span>◆</span><h2>Image workspace</h2><p>Import an image to inspect it here.</p></div>

  return <div className={`image-workspace ${editorMode ? 'palette-canvas' : ''}`}>
    <p className="visually-hidden" aria-live="polite">{announcement}</p>
    <div className="viewport-toolbar" inert={sheetOpen}><span>{before?.name}</span><strong>{editorMode ? canvasLabel : mode === 'single' ? 'Inspect' : mode === 'split' ? 'Split comparison' : 'Blink comparison'}</strong>{onClose && <button className="inspector-close" onClick={onClose}>Close inspector</button>}{!editorMode && <button ref={controlsButton} onClick={() => setSheetOpen(true)}>Controls</button>}</div>
    <div ref={viewport} className={`viewport ${onSelectPoint ? 'selecting-point' : ''} ${paintMode ? 'painting' : ''} ${spacePan ? 'pan-ready' : ''} ${panDragging ? 'pan-active' : ''}`} inert={sheetOpen} aria-busy={busy} aria-label={selectionLabel || 'Image comparison viewport'} onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp} onPointerCancel={pointerUp} onPointerLeave={() => setHoverPixel(null)} onWheel={wheel}>
      <div className="image-stage" style={transformStyle}>
        {before && <img className={before.mediaType === 'image/svg+xml' ? 'vector' : undefined} src={before.url} alt={before.name} draggable={false} onLoad={event => beforeSettled(event, true)} onError={event => beforeSettled(event, false)} />}
        {showAfter && compatibleAfter && <div className="compare-layer" style={mode === 'split' ? { clipPath: `inset(0 ${size.width - splitPosition}px 0 0)` } : undefined}><img className={compatibleAfter.mediaType === 'image/svg+xml' ? 'vector' : undefined} src={compatibleAfter.url} alt={compatibleAfter.name} draggable={false} /></div>}
        {compatibleInspections.map(layer => <img key={layer.id} className={`inspection-overlay ${layer.role}`} src={layer.url} alt={layer.name} draggable={false} />)}
        {tracePreview.stageLayer}
      </div>
      {overlay && <canvas ref={overlayCanvas} className="editor-overlay" aria-hidden="true" />}
      {mode === 'split' && compatibleAfter && <div
        className="split-divider"
        role="slider"
        tabIndex={0}
        aria-label="Comparison split position"
        aria-orientation="horizontal"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(split)}
        aria-valuetext={`${Math.round(split)}%`}
        style={{ left: `${split}%` }}
        onPointerDown={dividerPointerDown}
        onPointerMove={dividerPointerMove}
        onPointerUp={dividerPointerUp}
        onPointerCancel={dividerPointerUp}
        onKeyDown={dividerKeyDown}
      ><span className="split-line" aria-hidden="true" /></div>}
      {paintMode && !spacePan && hoverPixel && <div className="pencil-hover" style={{ left: transform.x + hoverPixel[0] * transform.scale, top: transform.y + hoverPixel[1] * transform.scale, width: transform.scale, height: transform.scale }} aria-hidden="true" />}
      {editorMode && inspectionToggles && inspections.length > 0 && <div className="canvas-inspections" role="group" aria-label="Inspection layers" onPointerDown={event => event.stopPropagation()}>
        {inspections.map(layer => <label key={layer.id}><input type="checkbox" checked={Boolean(enabledInspections[inspectionKey(layer)])} onChange={event => setEnabledInspections(value => ({ ...value, [inspectionKey(layer)]: event.target.checked }))} />{layer.name}</label>)}
        {incompatibleInspections.length > 0 && <p className="layer-warning" role="status">Hidden because its pixel dimensions differ: {incompatibleInspections.map(layer => layer.name).join(', ')}</p>}
      </div>}
      {busy && <div className="canvas-busy" aria-hidden="true"><LoaderCircle /><span>Loading image…</span></div>}
      {tracePreview.controls}
      <div className="canvas-tools" onPointerDown={event => event.stopPropagation()}>
        <button type="button" className="canvas-tool" aria-label="Zoom in" title="Zoom in" onClick={() => zoomCentre(1.25)}><ZoomIn aria-hidden="true" /></button>
        <button type="button" className="canvas-tool" aria-label="Zoom out" title="Zoom out" onClick={() => zoomCentre(.8)}><ZoomOut aria-hidden="true" /></button>
        <button type="button" className="canvas-tool" aria-label="Fit image" title="Fit image" onClick={() => fit()}><Maximize aria-hidden="true" /></button>
      </div>
    </div>
    <div className="layer-probes" inert={sheetOpen} aria-hidden="true">{layers.map(layer => <img key={layer.id} src={layer.url} alt="" onLoad={event => imageLoaded(layer.id, event)} />)}</div>
    {!editorMode && <div className="desktop-view-controls" inert={sheetOpen}>{controls}</div>}
    {!editorMode && <div ref={sheetRoot} className={`bottom-sheet ${sheetOpen ? 'open' : ''}`} aria-hidden={!sheetOpen} inert={!sheetOpen}>
      <div className="sheet-backdrop" aria-hidden="true" onClick={() => setSheetOpen(false)} />
      <div ref={sheetPanel} className="sheet-panel" role="dialog" aria-modal="true" aria-labelledby={sheetTitleId}>
        <div className="sheet-handle" aria-hidden="true" />
        <div className="sheet-title"><strong id={sheetTitleId}>Workspace controls</strong><button ref={doneButton} onClick={() => setSheetOpen(false)}>Done</button></div>
        {controls}
      </div>
    </div>}
  </div>
}
