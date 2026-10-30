import { type KeyboardEvent as ReactKeyboardEvent, PointerEvent, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { LoaderCircle, Maximize, Pause, Play, ZoomIn, ZoomOut } from 'lucide-react'
import { clamp, defaultComparePair, distance, fitTransform, keepCentre, gridsRegister, lineageRegisters, midpoint, pixelLine, sameImageSize, splitPositionInImage, zoomAt, type ImageSize, type Point, type UpscaleLineage, type ViewTransform } from './workspace'
import type { OverlayFrame } from './regionSelection'
import { LiveTraceLayer, type LiveFaces } from './LiveTrace'
import type { PaletteColour } from './api'
import { CLEANUP_CHANGES_OVERLAY_KIND, useCleanupPreview, useShowChangesSwitch, type CleanupPreviewOptions } from './CleanupPreview'
import { NibEditor, type NibEditorProps } from './NibEditor'

// `sizeFrom` names the raster whose pixel grid a vector layer is drawn on: an SVG
// sized in physical units reports an intrinsic size in CSS pixels, not the pixel
// grid its viewBox (and every registered raster) uses.
// `kind` names what a layer is independently of its artifact id, so an inspection toggle
// keyed by it stays on when a new run's layer of the same kind replaces the old one.
export type WorkspaceLayer = { id: string; name: string; url: string; mediaType: string; role: 'image' | 'mask' | 'overlay'; sizeFrom?: string; kind?: string }
// The Trace canvas's live trace: the faces of the running trace received so far, the
// palette they are painted with, the colour the trace omits, and the traced grid (the
// map's pixel units).
export type CanvasLiveTrace = { faces: LiveFaces; palette: readonly PaletteColour[]; omitted: number | null; traced: ImageSize }
// The Cleanup canvas's live preview; `tracedSize` is null while the revision's grid is
// unknown, which pauses it.
export type CanvasCleanupPreview = Omit<CleanupPreviewOptions, 'tracedSize'> & { tracedSize: ImageSize | null }
// A chosen point drawn over the canvas at a constant on-screen size: `points` are pixels
// of the `markerGridFrom` layer (one point, or both points of a pair), `label` is the
// number of its row in the side-panel list, and `pending` marks a pair's first pick
// while its second is still to come.
// Why the main canvas's comparison images could not be loaded, shown in its Compare
// control with a Retry.
export type CanvasCompareStatus = { message: string; onRetry: () => void }
// The layer `id` is the source the upscaled source `upscaledId` was made from, with the
// upscale's recorded grids when the revision has them; without, the two images' own
// sizes stand in (the revision's kind and parent already prove the lineage).
export type CanvasCompareLineage = Partial<UpscaleLineage> & { id: string; upscaledId: string }
export type CanvasMarkerKind = 'protect' | 'preserve' | 'pair'
export type CanvasMarker = { id: string; kind: CanvasMarkerKind; label: string; points: [number, number][]; pending?: boolean }
const inspectionKey = (layer: WorkspaceLayer) => layer.kind ?? layer.id
// The switch of the Trace canvas's Nodes overlay, kept with the inspection toggles.
const NODES_KEY = 'path-nodes'
// The canvas can show nib's editor (`nodes`) on a trace revision's planar map: `revisionId`
// owns the map at `mapUrl`, absent for a revision with none (traced before t3), and
// `onSaved` receives the revision a save makes.
export type CanvasNodes = NibEditorProps
// Screen-space sizes of the marker shapes and the pick-mode crosshair arms, in CSS px.
const MARKER_RADIUS = 7
const AIM_GAP = 3
const AIM_ARM = 10
// The absolute URL an <img> resolves a layer's (possibly relative) URL to.
const absoluteUrl = (url: string) => new URL(url, document.baseURI).href
type CompareMode = 'single' | 'split' | 'blink'
const COMPARE_MODES: CompareMode[] = ['single', 'split', 'blink']
// The main canvas's comparison mode, remembered across stage changes and reloads.
export const CANVAS_COMPARE_STORAGE_KEY = 'vinylgen.canvasCompare'
function storedCompareMode(): CompareMode {
  try {
    const value = localStorage.getItem(CANVAS_COMPARE_STORAGE_KEY)
    return value === 'split' || value === 'blink' ? value : 'single'
  } catch { return 'single' }
}
// The canvas column width below which its Compare control is a toolbar button opening a
// popover. It is the canvas's own width, not the window's: the stage rail and the stage
// settings panel collapse independently of the window.
export const NARROW_CANVAS_WIDTH = 600
// A press that moves less than this (in CSS pixels) is a tap, not a drag.
const TAP_DISTANCE = 5
function storeCompareMode(mode: CompareMode) {
  try { localStorage.setItem(CANVAS_COMPARE_STORAGE_KEY, mode) } catch { /* storage unavailable */ }
}

export function ImageWorkspace({ layers, preferredLayerId, selectionLabel, onSelectPoint: selectPointRequested, paintMode: paintRequested = false, onPaint: paintHandler, overlay, editorMode = false, canvasLabel = 'Palette canvas', inspectionKinds = [], pickAim: aimRequested = false, markers = [], markerGridFrom, highlightedMarker = null, loading = false, liveTrace, cleanupPreview, livePreviewPaused = false, livePreviewStatus, compareBefore = [], compareLineage, compareStatus, compareRemembered = true, compareKey, compareLocksEditing = false, onComparingChange, nodes, onClose }: { layers: WorkspaceLayer[]; preferredLayerId?: string; selectionLabel?: string; onSelectPoint?: (point: [number, number]) => void; paintMode?: boolean; onPaint?: (x: number, y: number) => void; overlay?: OverlayFrame; editorMode?: boolean; canvasLabel?: string; inspectionKinds?: readonly string[]; pickAim?: boolean; markers?: CanvasMarker[]; markerGridFrom?: string; highlightedMarker?: string | null; loading?: boolean; liveTrace?: CanvasLiveTrace; cleanupPreview?: CanvasCleanupPreview; livePreviewPaused?: boolean; livePreviewStatus?: string; compareBefore?: readonly string[]; compareLineage?: CanvasCompareLineage; compareStatus?: CanvasCompareStatus; compareRemembered?: boolean; compareKey?: string; compareLocksEditing?: boolean; onComparingChange?: (comparing: boolean) => void; nodes?: CanvasNodes; onClose?: () => void }) {
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
  const readout = useRef<HTMLDivElement>(null)
  const [readoutFlip, setReadoutFlip] = useState({ x: false, y: false })
  const [transform, setTransform] = useState<ViewTransform>({ x: 0, y: 0, scale: 1 })
  // Whether the view is still the fitted one. A resize refits a fitted view (so a panel
  // collapsing enlarges the image) and otherwise keeps the user's zoom and centre.
  const fitted = useRef(true)
  const viewportSize = useRef<ImageSize | null>(null)
  function moveView(update: (value: ViewTransform) => ViewTransform) {
    fitted.current = false
    setTransform(update)
  }
  // A one-pointer pan still within a tap's distance of where it started is a tap's jitter:
  // it moves the image but leaves the view fitted.
  function panView(pointerId: number, update: (value: ViewTransform) => ViewTransform) {
    const start = pointerStarts.current.get(pointerId), here = pointers.current.get(pointerId)
    if (start && here && distance(start, here) < TAP_DISTANCE) setTransform(update)
    else moveView(update)
  }
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [beforeId, setBeforeId] = useState(() => defaultComparePair(layers.filter(layer => layer.role === 'image').map(layer => layer.id), preferredLayerId).beforeId)
  const [afterId, setAfterId] = useState(() => defaultComparePair(layers.filter(layer => layer.role === 'image').map(layer => layer.id), preferredLayerId).afterId)
  const previousPreferredLayerId = useRef(preferredLayerId)
  const [mode, setMode] = useState<CompareMode>('single')
  // The main canvas's comparison: its remembered mode ('single' is Off) and the Before
  // chosen from `compareBefore`.
  const [canvasMode, setCanvasMode] = useState<CompareMode>(storedCompareMode)
  // A canvas that does not remember the mode (`compareRemembered` false) opens with it Off
  // and keeps its choice only while it stays that canvas, the workflow stage `compareKey`.
  const [sessionMode, setSessionMode] = useState<CompareMode>('single')
  useEffect(() => { setSessionMode('single') }, [compareRemembered, compareKey])
  const chosenMode = compareRemembered ? canvasMode : sessionMode
  const root = useRef<HTMLDivElement>(null)
  const [narrowCanvas, setNarrowCanvas] = useState(false)
  const [compareOpen, setCompareOpen] = useState(false)
  const comparePanel = useRef<HTMLDivElement>(null)
  const compareToggle = useRef<HTMLButtonElement>(null)
  const comparePanelId = useId()
  const [canvasBeforeId, setCanvasBeforeId] = useState<string | null>(null)
  const [split, setSplit] = useState(50)
  const [blinkOverlay, setBlinkOverlay] = useState(false)
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
  // An editor canvas offers (and draws) only its own overlay kinds, `inspectionKinds`, so
  // an overlay switched on in one stage never sticks on another; it returns with its
  // stage. The inspector offers every mask and overlay.
  const inspections = layers.filter(layer => (layer.role === 'mask' || layer.role === 'overlay')
    && (!editorMode || inspectionKinds.includes(inspectionKey(layer))))
  // On the Cleanup canvas the "Show changes" switch, not a toggle of its own, draws the
  // revision's changes overlay.
  const [showChanges, setShowChanges] = useShowChangesSwitch()
  const switchedKind = cleanupPreview ? CLEANUP_CHANGES_OVERLAY_KIND : null
  const appliedChanges = inspections.find(layer => inspectionKey(layer) === switchedKind)
  const toggledInspections = inspections.filter(layer => layer !== appliedChanges)
  const inspectionOn = (layer: WorkspaceLayer) => layer === appliedChanges ? showChanges : Boolean(enabledInspections[inspectionKey(layer)])
  const beforeSize = before ? dimensions[before.id] : undefined
  const compatibleAfter = after && before && after.id !== before.id && beforeSize && sameImageSize(beforeSize, dimensions[after.id]) ? after : undefined
  const comparableImages = before ? visible.filter(layer => layer.id !== before.id && beforeSize && sameImageSize(beforeSize, dimensions[layer.id])) : []
  const comparisonAvailable = comparableImages.length > 0
  // The main canvas compares the image it shows (the After; `before` above names the
  // layer that sizes the stage) with a Before from `compareBefore`, natural input first:
  // a distinct image whose grid registers with the shown one (`gridsRegister`), or the
  // pre-upscale source through its recorded upscale (`compareLineage`); either covers
  // the same extent and is stretched over it. With none, it offers no comparison.
  const upscaleBefore = compareLineage?.before ?? (compareLineage ? dimensions[compareLineage.id] : undefined)
  const upscaleAfter = compareLineage?.after ?? (compareLineage ? dimensions[compareLineage.upscaledId] : undefined)
  const upscaleLineage = upscaleBefore && upscaleAfter ? { before: upscaleBefore, after: upscaleAfter } : undefined
  const canvasCandidates = editorMode && before && beforeSize
    ? [...new Set(compareBefore)].flatMap(id => {
      const layer = visible.find(item => item.id === id)
      const registers = layer && (gridsRegister(beforeSize, dimensions[layer.id])
        || (layer.id === compareLineage?.id && lineageRegisters(beforeSize, dimensions[layer.id], upscaleLineage)))
      return layer && layer.id !== before.id && registers ? [layer] : []
    }) : []
  const canvasBefore = canvasCandidates.find(layer => layer.id === canvasBeforeId) ?? canvasCandidates[0]
  // The layer split and blink draw over the stage's own image: the inspector's After, or
  // the main canvas's Before.
  const compareLayer = editorMode ? canvasBefore : compatibleAfter
  const activeMode: CompareMode = editorMode ? (canvasBefore ? chosenMode : 'single') : mode
  // While the main canvas compares, its Before may be what is on screen, so nothing edits
  // it (area selection, pencil, preservation picking) and nothing is drawn over the pair
  // (inspection overlays, live previews, the recolour preview); each returns, with its
  // switch as it was, once Compare is Off.
  const comparing = editorMode && activeMode !== 'single'
  const onSelectPoint = comparing ? undefined : selectPointRequested
  const paintMode = !comparing && paintRequested
  const onPaint = comparing ? undefined : paintHandler
  const pickAim = !comparing && aimRequested
  const editingRequested = Boolean(selectPointRequested || paintRequested || aimRequested)
  useEffect(() => {
    onComparingChange?.(comparing)
    return () => onComparingChange?.(false)
  }, [comparing])
  // An enabled overlay draws over the shown image when their grids register: the same
  // grid, or one an integer upscale of the other (a flattened grid with
  // `flatten_upscale` > 1 over its source), stretched over the same extent.
  const compatibleInspections = inspections.filter(layer => inspectionOn(layer) && beforeSize && gridsRegister(beforeSize, dimensions[layer.id]))
  const incompatibleInspections = inspections.filter(layer => inspectionOn(layer) && beforeSize && dimensions[layer.id] && !gridsRegister(beforeSize, dimensions[layer.id]))
  const drawnInspections = comparing ? [] : compatibleInspections
  // The Nodes switch; the editor it turns on is settled with the live preview below.
  const nodesSwitched = Boolean(nodes) && Boolean(enabledInspections[NODES_KEY])

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
    if (activeMode !== 'blink' || blinkPaused) return
    const timer = window.setInterval(() => setBlinkOverlay(value => !value), 550)
    return () => window.clearInterval(timer)
  }, [activeMode, blinkPaused])

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
  }, [overlay, transform, comparing])

  useEffect(() => {
    const element = viewport.current
    if (!element || !size.width || !size.height || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(entries => {
      const bounds = entries[0]?.contentRect
      if (!bounds?.width || !bounds.height) return
      const previous = viewportSize.current
      const next = { width: bounds.width, height: bounds.height }
      viewportSize.current = next
      if (fitted.current || !previous) setTransform(fitTransform(next.width, next.height, size.width, size.height))
      else setTransform(value => keepCentre(value, previous, next))
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [size.width, size.height])

  function fit(width = size.width, height = size.height) {
    fitted.current = true
    setTransform(fitTransform(viewport.current?.clientWidth ?? 0, viewport.current?.clientHeight ?? 0, width, height))
  }

  function zoomCentre(factor: number) {
    moveView(value => zoomAt(value, { x: (viewport.current?.clientWidth ?? 0) / 2, y: (viewport.current?.clientHeight ?? 0) / 2 }, factor))
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
    if (paintMode || (pickAim && onSelectPoint)) setHoverPixel(panningPointer.current || spaceHeld.current ? null : imagePixel(event.clientX, event.clientY))
    const previous = pointers.current.get(event.pointerId)
    if (!previous) return
    const current = localPoint(event.clientX, event.clientY)
    pointers.current.set(event.pointerId, current)
    if (pointers.current.size === 2 && gesture.current) {
      const [a, b] = [...pointers.current.values()]
      const centre = midpoint(a, b)
      const zoomed = zoomAt(gesture.current.transform, gesture.current.centre, distance(a, b) / gesture.current.distance)
      const pinched = { ...zoomed, x: zoomed.x + centre.x - gesture.current.centre.x, y: zoomed.y + centre.y - gesture.current.centre.y }
      moveView(() => pinched)
    } else if (panningPointer.current) {
      panView(event.pointerId, value => ({ ...value, x: value.x + current.x - previous.x, y: value.y + current.y - previous.y }))
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
      panView(event.pointerId, value => ({ ...value, x: value.x + current.x - previous.x, y: value.y + current.y - previous.y }))
    }
  }

  function pointerUp(event: PointerEvent<HTMLDivElement>) {
    const start = pointerStarts.current.get(event.pointerId)
    const end = localPoint(event.clientX, event.clientY)
    if (onSelectPoint && start && distance(start, end) < TAP_DISTANCE && size.width && size.height) {
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

  // Wheel zoom must cancel the page scroll, which React's (passive) onWheel cannot, so
  // it is a native non-passive listener on the viewport.
  const hasImages = visible.length > 0
  useEffect(() => {
    const element = viewport.current
    if (!element) return
    const wheel = (event: WheelEvent) => {
      event.preventDefault()
      moveView(value => zoomAt(value, localPoint(event.clientX, event.clientY), Math.exp(-event.deltaY * 0.0015)))
    }
    element.addEventListener('wheel', wheel, { passive: false })
    return () => element.removeEventListener('wheel', wheel)
  }, [hasImages])

  function chooseMode(value: CompareMode) {
    if (!editorMode) setMode(value)
    else if (compareRemembered) { setCanvasMode(value); storeCompareMode(value) } else setSessionMode(value)
    if (value === 'blink') setBlinkOverlay(false)
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

  const showOverlay = activeMode === 'split' || (activeMode === 'blink' && blinkOverlay)
  // The compared layer is drawn crisp while it is enlarged on screen: its own
  // magnification (displayed over natural size), not the shown image's, since a Before
  // on a smaller grid is stretched further.
  const compareSize = compareLayer ? dimensions[compareLayer.id] : undefined
  const compareMagnified = Boolean(compareSize?.width) && size.width * transform.scale / compareSize!.width >= 1
  const transformStyle = { width: size.width, height: size.height, transform: `translate3d(${transform.x}px, ${transform.y}px, 0) scale(${transform.scale})` }
  // The Cleanup canvas's live preview of the zoomed-in area (inert without
  // `cleanupPreview`). Paused (while the revision's grid is unknown), the switch stays but
  // nothing is in view to preview, so no request is made, and nothing is drawn.
  const previewPaused = livePreviewPaused || !cleanupPreview?.tracedSize
  // While paused nothing is in view, so the grid given here is never read.
  const pausedGrid = { width: 1, height: 1 }
  const cleanupOptions: CleanupPreviewOptions | undefined = cleanupPreview ? { ...cleanupPreview, tracedSize: cleanupPreview.tracedSize ?? pausedGrid } : undefined
  // Comparing holds the live preview exactly as a pause does: nothing in view, so no
  // request (the one in flight is aborted), no badge and no live legend, until Off.
  const previewHeld = previewPaused || comparing
  const previewViewport = previewHeld ? { width: 0, height: 0 } : { width: viewport.current?.clientWidth ?? 0, height: viewport.current?.clientHeight ?? 0 }
  const cleanupLivePreview = useCleanupPreview(cleanupOptions, transform, previewViewport, size,
    { on: showChanges, set: setShowChanges, available: Boolean(appliedChanges), drawn: Boolean(appliedChanges && drawnInspections.includes(appliedChanges)) },
    previewHeld, livePreviewStatus)
  // The Nodes overlay is offered for a revision with a planar map: nib's editor on it, over
  // the canvas. Like every overlay it is hidden while comparing.
  const nodesAvailable = Boolean(nodes?.mapUrl)
  const nodesOn = nodesAvailable && nodesSwitched
  const nodesShown = nodesOn && !comparing
  const splitPosition = splitPositionInImage(viewport.current?.clientWidth ?? 0, split, size.width, transform)
  // Markers are placed on the shown image's grid, scaled from their own grid when the two
  // register (the source grid over a larger flattened raster); otherwise none is drawn.
  const markerGrid = markerGridFrom ? dimensions[markerGridFrom] : undefined
  const markerScale = markers.length && size.width && gridsRegister(size, markerGrid) ? size.width / markerGrid!.width : null
  const markerAt = ([x, y]: [number, number]) => ({
    x: transform.x + (x + .5) * markerScale! * transform.scale, y: transform.y + (y + .5) * markerScale! * transform.scale })
  // Pick mode outlines the hovered pixel with a crosshair and reads out its coordinates.
  const aim = pickAim && onSelectPoint && hoverPixel ? {
    x: transform.x + (hoverPixel[0] + .5) * transform.scale, y: transform.y + (hoverPixel[1] + .5) * transform.scale,
    half: Math.max(transform.scale / 2, 2) } : null
  // The readout sits below-right of the aimed pixel, flipping to the other side where its
  // measured box would cross the viewport's right or bottom edge; a flipped side is
  // anchored at its far corner by translate(-100%), so no text width is assumed.
  const readoutOffset = aim ? aim.half + AIM_GAP + 4 : 0
  const readoutPlacement = aim ? {
    left: readoutFlip.x ? aim.x - readoutOffset : aim.x + readoutOffset,
    top: readoutFlip.y ? aim.y - readoutOffset : aim.y + readoutOffset,
    transform: readoutFlip.x || readoutFlip.y ? `translate(${readoutFlip.x ? '-100%' : '0'}, ${readoutFlip.y ? '-100%' : '0'})` : undefined,
  } : undefined
  useLayoutEffect(() => {
    const element = readout.current
    if (!aim || !element) return
    const x = aim.x + readoutOffset + element.offsetWidth > (viewport.current?.clientWidth ?? Infinity)
    const y = aim.y + readoutOffset + element.offsetHeight > (viewport.current?.clientHeight ?? Infinity)
    if (x !== readoutFlip.x || y !== readoutFlip.y) setReadoutFlip({ x, y })
  })
  const annotations = (markerScale !== null || aim) && <svg className="canvas-annotations" aria-hidden="true">
    {markerScale !== null && markers.map(marker => {
      const at = marker.points.map(markerAt)
      const [a, b] = at
      const span = a && b ? Math.hypot(b.x - a.x, b.y - a.y) : 0
      const trim = span > 2 * MARKER_RADIUS ? MARKER_RADIUS / span : null
      return <g key={marker.id} className={`canvas-marker ${marker.kind}${marker.pending ? ' pending' : ''}${marker.id === highlightedMarker ? ' highlighted' : ''}`} data-marker-id={marker.id}>
        {trim !== null && <g className="marker-link">
          {['marker-halo', 'marker-shape'].map(layer => <line key={layer} className={layer} x1={a.x + (b.x - a.x) * trim} y1={a.y + (b.y - a.y) * trim} x2={b.x - (b.x - a.x) * trim} y2={b.y - (b.y - a.y) * trim} />)}
        </g>}
        {at.map((point, index) => <g key={index} className="marker-point" transform={`translate(${point.x} ${point.y})`}>
          {['marker-halo', 'marker-shape'].map(layer => marker.kind === 'protect'
            ? <polygon key={layer} className={layer} points={`0,${-MARKER_RADIUS - 1} ${MARKER_RADIUS + 1},0 0,${MARKER_RADIUS + 1} ${-MARKER_RADIUS - 1},0`} />
            : marker.kind === 'preserve'
              ? <circle key={layer} className={layer} r={MARKER_RADIUS} />
              : <rect key={layer} className={layer} x={-MARKER_RADIUS + 1} y={-MARKER_RADIUS + 1} width={2 * MARKER_RADIUS - 2} height={2 * MARKER_RADIUS - 2} />)}
          <text className="marker-label" x={MARKER_RADIUS + 3} y={-MARKER_RADIUS - 1}>{marker.label}</text>
        </g>)}
      </g>
    })}
    {aim && <g className="pick-aim">
      {['marker-halo', 'marker-shape'].map(layer => <g key={layer} className={layer}>
        <rect className="pick-hover" x={aim.x - aim.half} y={aim.y - aim.half} width={2 * aim.half} height={2 * aim.half} />
        <line x1={aim.x - aim.half - AIM_GAP - AIM_ARM} y1={aim.y} x2={aim.x - aim.half - AIM_GAP} y2={aim.y} />
        <line x1={aim.x + aim.half + AIM_GAP} y1={aim.y} x2={aim.x + aim.half + AIM_GAP + AIM_ARM} y2={aim.y} />
        <line x1={aim.x} y1={aim.y - aim.half - AIM_GAP - AIM_ARM} x2={aim.x} y2={aim.y - aim.half - AIM_GAP} />
        <line x1={aim.x} y1={aim.y + aim.half + AIM_GAP} x2={aim.x} y2={aim.y + aim.half + AIM_GAP + AIM_ARM} />
      </g>)}
    </g>}
  </svg>

  const controls = <>
    {!editorMode && <section className="control-section">
      <h3>Compare</h3>
      <div className="segmented" aria-label="Comparison mode">
        {COMPARE_MODES.map(value => {
          const locked = value !== 'single' && !comparisonAvailable
          return <button key={value} className={mode === value ? 'active' : ''} aria-pressed={mode === value} disabled={locked} aria-disabled={locked} title={locked ? 'Comparison needs a second image with matching pixel dimensions.' : undefined} onClick={() => chooseMode(value)}>{value}</button>
        })}
      </div>
      {!comparisonAvailable && <p className="layer-warning" role="status">Comparison needs a second image with matching pixel dimensions.</p>}
      <label>Before<select value={before?.id ?? ''} onChange={event => { setBeforeId(event.target.value); setSize({ width: 0, height: 0 }) }}>{visible.map(layer => <option key={layer.id} value={layer.id}>{layer.name}</option>)}</select></label>
      <label>After<select value={after?.id ?? ''} onChange={event => setAfterId(event.target.value)}>{visible.map(layer => <option key={layer.id} value={layer.id} disabled={Boolean(beforeSize && dimensions[layer.id] && !sameImageSize(beforeSize, dimensions[layer.id]))}>{layer.name}{beforeSize && dimensions[layer.id] && !sameImageSize(beforeSize, dimensions[layer.id]) ? ' · different canvas' : ''}</option>)}</select></label>
      {after && beforeSize && dimensions[after.id] && !compatibleAfter && <p className="layer-warning" role="status">Comparison hidden: the selected images have different pixel dimensions and cannot be registered exactly.</p>}
      {mode === 'split' && <label>Split · {split}%<input aria-label="Split position" type="range" min="0" max="100" value={split} onChange={event => setSplit(Number(event.target.value))} /></label>}
      {mode === 'blink' && <div className="view-actions">
        <button onClick={() => setBlinkPaused(value => !value)}>{blinkPaused ? 'Resume blink' : 'Pause blink'}</button>
        <button onClick={() => setBlinkOverlay(value => !value)}>Show {blinkOverlay ? 'before' : 'after'} image</button>
        {reducedMotion && blinkPaused && <p role="status">Blink is paused to respect your reduced-motion preference.</p>}
      </div>}
    </section>}
    {!editorMode && toggledInspections.length > 0 && <section className="control-section inspection-controls">
      <h3>Masks &amp; inspection layers</h3>
      {toggledInspections.map(layer => <label key={layer.id}><input type="checkbox" checked={Boolean(enabledInspections[inspectionKey(layer)])} onChange={event => setEnabledInspections(value => ({ ...value, [inspectionKey(layer)]: event.target.checked }))} />{layer.name}</label>)}
      {incompatibleInspections.length > 0 && <p className="layer-warning" role="status">Hidden because its pixel dimensions differ: {incompatibleInspections.map(layer => layer.name).join(', ')}</p>}
    </section>}
  </>

  // The main canvas's Compare control, offered where a Before image covers the shown
  // one's extent, or to report (with a Retry) that its images could not be loaded. As in
  // the inspector, split draws the After (the shown image) left of the divider and the
  // Before right of it; blink alternates the two.
  const hiddenWhileComparing = inspections.some(inspectionOn) || nodesOn || Boolean(overlay) || Boolean(liveTrace || cleanupLivePreview.stageLayer)
  // In a canvas narrower than NARROW_CANVAS_WIDTH the control is one toolbar button
  // opening it as a popover positioned from the canvas (outside the toolbar row, which
  // clips), so the toolbar stays one row; wider, it sits inline in the toolbar.
  useEffect(() => {
    const element = root.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(entries => {
      const width = entries[0]?.contentRect.width
      if (width) setNarrowCanvas(width < NARROW_CANVAS_WIDTH)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [hasImages])
  const popover = editorMode && narrowCanvas
  const compareWord = !canvasBefore ? 'Unavailable' : chosenMode === 'single' ? 'Off' : chosenMode === 'split' ? 'Split' : 'Blink'
  const insideCompare = (node: EventTarget | null) => node instanceof Node && Boolean(comparePanel.current?.contains(node) || compareToggle.current?.contains(node))
  function compareKeyDown(event: ReactKeyboardEvent<HTMLElement>) {
    if (event.key !== 'Escape' || !compareOpen) return
    event.preventDefault()
    setCompareOpen(false)
    compareToggle.current?.focus()
  }
  // Focus moving on to anything but the toggle or the popover (Tab past its last
  // control) closes it; a press inside that focuses nothing leaves it open.
  function compareBlur(event: React.FocusEvent<HTMLElement>) {
    if (event.relatedTarget && !insideCompare(event.relatedTarget)) setCompareOpen(false)
  }
  const compareToggleButton = popover && (canvasBefore || compareStatus) && <button ref={compareToggle} type="button" className="canvas-compare-toggle" aria-label={`Compare: ${compareWord}${compareStatus ? ' (failed)' : ''}`} aria-expanded={compareOpen} aria-controls={comparePanelId} onClick={() => setCompareOpen(open => !open)} onKeyDown={compareKeyDown} onBlur={compareBlur}><span className="canvas-compare-toggle-prefix" aria-hidden="true">Compare: </span>{compareWord}{compareStatus ? ' !' : ''}</button>
  const canvasCompare = editorMode && (canvasBefore || compareStatus) && <div ref={comparePanel} id={comparePanelId} className={`canvas-compare${popover ? ' popover' : ''}`} role="group" aria-label="Compare" hidden={popover && !compareOpen} onKeyDown={popover ? compareKeyDown : undefined} onBlur={popover ? compareBlur : undefined}>
    <span className="canvas-compare-title" aria-hidden="true">Compare</span>
    {compareStatus && <p className="canvas-compare-note error" role="alert">{compareStatus.message} <button type="button" className="canvas-compare-action" onClick={compareStatus.onRetry}>Retry</button></p>}
    {canvasBefore && <><div className="segmented" role="group" aria-label="Comparison mode">
      {COMPARE_MODES.map(value => <button key={value} type="button" className={chosenMode === value ? 'active' : ''} aria-pressed={chosenMode === value} onClick={() => chooseMode(value)}>{value === 'single' ? 'Off' : value}</button>)}
    </div>
    {activeMode !== 'single' && (canvasCandidates.length > 1
      ? <label className="canvas-compare-before">Before<select value={canvasBefore.id} onChange={event => setCanvasBeforeId(event.target.value)}>{canvasCandidates.map(layer => <option key={layer.id} value={layer.id}>{layer.name}</option>)}</select></label>
      : <span className="canvas-compare-before" title={canvasBefore.name}>Before: {canvasBefore.name}</span>)}
    {activeMode === 'blink' && <>
      <button type="button" className="canvas-compare-action" aria-label={blinkPaused ? 'Resume blink' : 'Pause blink'} title={blinkPaused ? 'Resume blink' : 'Pause blink'} onClick={() => setBlinkPaused(value => !value)}>{blinkPaused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}</button>
      <button type="button" className="canvas-compare-action" onClick={() => setBlinkOverlay(value => !value)}>Show {blinkOverlay ? 'after' : 'before'}</button>
    </>}
    {comparing && (editingRequested || compareLocksEditing) && <p className="canvas-compare-note">Turn Compare off to edit</p>}
    {comparing && hiddenWhileComparing && <p className="canvas-compare-note">Overlays and live preview hidden while comparing</p>}
    </>}
    <p className="visually-hidden" role="status">{activeMode === 'blink' && reducedMotion && blinkPaused ? 'Blink is paused to respect your reduced-motion preference.' : ''}</p>
  </div>

  if (!visible.length) return <div className="image-workspace empty-workspace"><span>◆</span><h2>Image workspace</h2><p>Import an image to inspect it here.</p></div>

  return <div ref={root} className={`image-workspace ${editorMode ? 'palette-canvas' : ''} ${popover ? 'narrow-canvas' : ''}`}>
    <p className="visually-hidden" aria-live="polite">{announcement}</p>
    <div className="viewport-toolbar" inert={sheetOpen}><span>{loading ? 'Loading…' : before?.name}</span>{popover ? compareToggleButton : canvasCompare}<strong>{editorMode ? canvasLabel : mode === 'single' ? 'Inspect' : mode === 'split' ? 'Split comparison' : 'Blink comparison'}</strong>{onClose && <button className="inspector-close" onClick={onClose}>Close inspector</button>}{!editorMode && <button ref={controlsButton} onClick={() => setSheetOpen(true)}>Controls</button>}</div>
    {/* While the popover is open a transparent backdrop covers the page beneath it: a
        press there closes the popover on its click (the whole press, touch included, lands
        on the backdrop), so it reaches nothing underneath. */}
    {popover && compareOpen && <div className="canvas-compare-backdrop" aria-hidden="true" onClick={() => setCompareOpen(false)} />}
    {popover && canvasCompare}
    <div ref={viewport} className={`viewport ${comparing ? 'comparing' : ''} ${onSelectPoint ? 'selecting-point' : ''} ${paintMode ? 'painting' : ''} ${spacePan ? 'pan-ready' : ''} ${panDragging ? 'pan-active' : ''}`} inert={sheetOpen} aria-busy={busy} aria-label={selectionLabel || 'Image comparison viewport'} onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp} onPointerCancel={pointerUp} onPointerLeave={() => setHoverPixel(null)}>
      <div className={`image-stage ${transform.scale >= 1 ? 'magnified' : ''}`} style={transformStyle}>
        {before && <img className={before.mediaType === 'image/svg+xml' ? 'vector' : undefined} src={before.url} alt={before.name} draggable={false} onLoad={event => beforeSettled(event, true)} onError={event => beforeSettled(event, false)} />}
        {showOverlay && compareLayer && <div className="compare-layer" style={activeMode === 'split' ? { clipPath: editorMode ? `inset(0 0 0 ${splitPosition}px)` : `inset(0 ${size.width - splitPosition}px 0 0)` } : undefined}><img className={[compareLayer.mediaType === 'image/svg+xml' ? 'vector' : '', compareMagnified ? 'magnified' : ''].filter(Boolean).join(' ') || undefined} src={compareLayer.url} alt={compareLayer.name} draggable={false} /></div>}
        {drawnInspections.map(layer => <img key={layer.id} className={`inspection-overlay ${layer.role}`} src={layer.url} alt={layer.name} draggable={false} />)}
        {!comparing && liveTrace && <LiveTraceLayer {...liveTrace} />}
        {!previewHeld && cleanupLivePreview.stageLayer}
      </div>
      {overlay && !comparing && <canvas ref={overlayCanvas} className="editor-overlay" aria-hidden="true" />}
      {nodesShown && nodes && <NibEditor revisionId={nodes.revisionId} mapUrl={nodes.mapUrl} onSaved={nodes.onSaved} />}
      {activeMode === 'split' && compareLayer && <div
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
      {annotations}
      {aim && hoverPixel && <div ref={readout} className="pick-readout" aria-hidden="true" style={readoutPlacement}>x {hoverPixel[0]}, y {hoverPixel[1]}</div>}
      {paintMode && !spacePan && hoverPixel && <div className="pencil-hover" style={{ left: transform.x + hoverPixel[0] * transform.scale, top: transform.y + hoverPixel[1] * transform.scale, width: transform.scale, height: transform.scale }} aria-hidden="true" />}
      {editorMode && (toggledInspections.length > 0 || nodesAvailable) && <div className="canvas-inspections" role="group" aria-label="Inspection layers" onPointerDown={event => event.stopPropagation()}>
        {toggledInspections.map(layer => <label key={layer.id}><input type="checkbox" checked={Boolean(enabledInspections[inspectionKey(layer)])} onChange={event => setEnabledInspections(value => ({ ...value, [inspectionKey(layer)]: event.target.checked }))} />{layer.name}</label>)}
        {nodesAvailable && <label><input type="checkbox" checked={nodesOn} onChange={event => setEnabledInspections(value => ({ ...value, [NODES_KEY]: event.target.checked }))} />Nodes</label>}
        {incompatibleInspections.length > 0 && <p className="layer-warning" role="status">Hidden because its pixel dimensions differ: {incompatibleInspections.map(layer => layer.name).join(', ')}</p>}
      </div>}
      {busy && <div className="canvas-busy" aria-hidden="true"><LoaderCircle /><span>Loading image…</span></div>}
      {cleanupLivePreview.controls}
      {!nodesShown && <div className="canvas-tools" onPointerDown={event => event.stopPropagation()}>
        <button type="button" className="canvas-tool" aria-label="Zoom in" title="Zoom in" onClick={() => zoomCentre(1.25)}><ZoomIn aria-hidden="true" /></button>
        <button type="button" className="canvas-tool" aria-label="Zoom out" title="Zoom out" onClick={() => zoomCentre(.8)}><ZoomOut aria-hidden="true" /></button>
        <button type="button" className="canvas-tool" aria-label="Fit image" title="Fit image" onClick={() => fit()}><Maximize aria-hidden="true" /></button>
      </div>}
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
