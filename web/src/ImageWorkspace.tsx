import { PointerEvent, WheelEvent, useEffect, useId, useRef, useState } from 'react'
import { distance, fitTransform, midpoint, pixelLine, sameImageSize, splitPositionInImage, zoomAt, type ImageSize, type Point, type ViewTransform } from './workspace'

export type WorkspaceLayer = { id: string; name: string; url: string; mediaType: string; role: 'image' | 'mask' | 'overlay' }
type CompareMode = 'single' | 'split' | 'blink'

export function ImageWorkspace({ layers, preferredLayerId, selectionLabel, onSelectPoint, paintMode = false, onPaint, overlay, editorMode = false, onClose }: { layers: WorkspaceLayer[]; preferredLayerId?: string; selectionLabel?: string; onSelectPoint?: (point: [number, number]) => void; paintMode?: boolean; onPaint?: (x: number, y: number) => void; overlay?: string; editorMode?: boolean; onClose?: () => void }) {
  const viewport = useRef<HTMLDivElement>(null)
  const pointers = useRef(new Map<number, Point>())
  const pointerStarts = useRef(new Map<number, Point>())
  const gesture = useRef<{ distance: number; centre: Point; transform: ViewTransform } | null>(null)
  const painting = useRef(false)
  const lastPaint = useRef<[number, number] | null>(null)
  const [transform, setTransform] = useState<ViewTransform>({ x: 0, y: 0, scale: 1 })
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [beforeId, setBeforeId] = useState(() => layers.some(layer => layer.role === 'image' && layer.id === preferredLayerId) ? preferredLayerId! : layers.find(layer => layer.role === 'image')?.id ?? '')
  const [afterId, setAfterId] = useState(() => layers.filter(layer => layer.role === 'image').at(-1)?.id ?? '')
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
  const [dimensions, setDimensions] = useState<Record<string, ImageSize>>({})
  const [enabledInspections, setEnabledInspections] = useState<Record<string, boolean>>({})

  const visible = layers.filter(layer => layer.role === 'image')
  const before = visible.find(layer => layer.id === beforeId) ?? visible[0]
  const after = visible.find(layer => layer.id === afterId) ?? visible[visible.length - 1]
  const inspections = layers.filter(layer => layer.role === 'mask' || layer.role === 'overlay')
  const beforeSize = before ? dimensions[before.id] : undefined
  const compatibleAfter = after && beforeSize && sameImageSize(beforeSize, dimensions[after.id]) ? after : undefined
  const compatibleInspections = inspections.filter(layer => enabledInspections[layer.id] && beforeSize && sameImageSize(beforeSize, dimensions[layer.id]))
  const incompatibleInspections = inspections.filter(layer => enabledInspections[layer.id] && beforeSize && dimensions[layer.id] && !sameImageSize(beforeSize, dimensions[layer.id]))

  useEffect(() => {
    if (!visible.some(layer => layer.id === beforeId) && visible[0]) setBeforeId(visible[0].id)
    if (!visible.some(layer => layer.id === afterId) && visible.length) setAfterId(visible[visible.length - 1].id)
  }, [layers, beforeId, afterId])

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

  function imageLoaded(layerId: string, event: React.SyntheticEvent<HTMLImageElement>) {
    const image = event.currentTarget
    if (image.naturalWidth) setDimensions(value => ({ ...value, [layerId]: { width: image.naturalWidth, height: image.naturalHeight } }))
    if (layerId === before?.id && (!size.width || size.width !== image.naturalWidth || size.height !== image.naturalHeight) && image.naturalWidth) {
      const next = { width: image.naturalWidth, height: image.naturalHeight }
      setSize(next)
      fit(next.width, next.height)
    }
  }

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
      const [a, b] = [...pointers.current.values()]
      gesture.current = { distance: distance(a, b), centre: midpoint(a, b), transform }
    } else if (paintMode && onPaint) {
      const pixel = imagePixel(event.clientX, event.clientY)
      painting.current = true
      lastPaint.current = pixel
      if (pixel) onPaint(pixel[0], pixel[1])
    }
  }

  function pointerMove(event: PointerEvent<HTMLDivElement>) {
    const previous = pointers.current.get(event.pointerId)
    if (!previous) return
    const current = localPoint(event.clientX, event.clientY)
    pointers.current.set(event.pointerId, current)
    if (pointers.current.size === 2 && gesture.current) {
      const [a, b] = [...pointers.current.values()]
      const centre = midpoint(a, b)
      const zoomed = zoomAt(gesture.current.transform, gesture.current.centre, distance(a, b) / gesture.current.distance)
      setTransform({ ...zoomed, x: zoomed.x + centre.x - gesture.current.centre.x, y: zoomed.y + centre.y - gesture.current.centre.y })
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
    pointers.current.delete(event.pointerId)
    pointerStarts.current.delete(event.pointerId)
    gesture.current = null
  }

  function wheel(event: WheelEvent<HTMLDivElement>) {
    event.preventDefault()
    setTransform(value => zoomAt(value, localPoint(event.clientX, event.clientY), Math.exp(-event.deltaY * 0.0015)))
  }

  const showAfter = mode === 'split' || (mode === 'blink' && blinkAfter)
  const transformStyle = { width: size.width, height: size.height, transform: `translate3d(${transform.x}px, ${transform.y}px, 0) scale(${transform.scale})` }
  const splitPosition = splitPositionInImage(viewport.current?.clientWidth ?? 0, split, size.width, transform)

  const controls = <>
    {!editorMode && <section className="control-section">
      <h3>Compare</h3>
      <div className="segmented" aria-label="Comparison mode">
        {(['single', 'split', 'blink'] as CompareMode[]).map(value => <button key={value} className={mode === value ? 'active' : ''} aria-pressed={mode === value} onClick={() => { setMode(value); if (value === 'blink') setBlinkAfter(false) }}>{value}</button>)}
      </div>
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
      {inspections.map(layer => <label key={layer.id}><input type="checkbox" checked={Boolean(enabledInspections[layer.id])} onChange={event => setEnabledInspections(value => ({ ...value, [layer.id]: event.target.checked }))} />{layer.name}</label>)}
      {incompatibleInspections.length > 0 && <p className="layer-warning" role="status">Hidden because its pixel dimensions differ: {incompatibleInspections.map(layer => layer.name).join(', ')}</p>}
    </section>}
    <section className="control-section view-actions">
      <h3>View</h3>
      <div><button onClick={() => setTransform(value => zoomAt(value, { x: (viewport.current?.clientWidth ?? 0) / 2, y: (viewport.current?.clientHeight ?? 0) / 2 }, 1.25))}>Zoom in</button><button onClick={() => setTransform(value => zoomAt(value, { x: (viewport.current?.clientWidth ?? 0) / 2, y: (viewport.current?.clientHeight ?? 0) / 2 }, .8))}>Zoom out</button></div>
      <button onClick={() => fit()}>Fit image</button>
      <p>{Math.round(transform.scale * 100)}% · drag to pan · pinch or wheel to zoom</p>
    </section>
  </>

  if (!visible.length) return <div className="image-workspace empty-workspace"><span>◆</span><h2>Image workspace</h2><p>Import an image to inspect it here.</p></div>

  return <div className={`image-workspace ${editorMode ? 'palette-canvas' : ''}`}>
    <div className="viewport-toolbar" inert={sheetOpen}><span>{before?.name}</span><strong>{editorMode ? 'Palette canvas' : mode === 'single' ? 'Inspect' : mode === 'split' ? 'Split comparison' : 'Blink comparison'}</strong>{onClose && <button className="inspector-close" onClick={onClose}>Close inspector</button>}<button ref={controlsButton} onClick={() => setSheetOpen(true)}>Controls</button></div>
    <div ref={viewport} className={`viewport ${onSelectPoint ? 'selecting-point' : ''} ${paintMode ? 'painting' : ''}`} inert={sheetOpen} aria-label={selectionLabel || 'Image comparison viewport'} onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp} onPointerCancel={pointerUp} onWheel={wheel}>
      <div className="image-stage" style={transformStyle}>
        {before && <img src={before.url} alt={before.name} draggable={false} onLoad={event => imageLoaded(before.id, event)} />}
        {showAfter && compatibleAfter && <div className="compare-layer" style={mode === 'split' ? { clipPath: `inset(0 ${size.width - splitPosition}px 0 0)` } : undefined}><img src={compatibleAfter.url} alt={compatibleAfter.name} draggable={false} /></div>}
        {compatibleInspections.map(layer => <img key={layer.id} className={`inspection-overlay ${layer.role}`} src={layer.url} alt={layer.name} draggable={false} />)}
        {overlay && <img className="editor-overlay" src={overlay} alt="" aria-hidden="true" draggable={false} />}
      </div>
      {mode === 'split' && <div className="split-line" style={{ left: `${split}%` }} aria-hidden="true" />}
    </div>
    <div className="layer-probes" inert={sheetOpen} aria-hidden="true">{layers.map(layer => <img key={layer.id} src={layer.url} alt="" onLoad={event => imageLoaded(layer.id, event)} />)}</div>
    <div className="desktop-view-controls" inert={sheetOpen}>{controls}</div>
    <div ref={sheetRoot} className={`bottom-sheet ${sheetOpen ? 'open' : ''}`} aria-hidden={!sheetOpen} inert={!sheetOpen}>
      <div className="sheet-backdrop" aria-hidden="true" onClick={() => setSheetOpen(false)} />
      <div ref={sheetPanel} className="sheet-panel" role="dialog" aria-modal="true" aria-labelledby={sheetTitleId}>
        <div className="sheet-handle" aria-hidden="true" />
        <div className="sheet-title"><strong id={sheetTitleId}>Workspace controls</strong><button ref={doneButton} onClick={() => setSheetOpen(false)}>Done</button></div>
        {controls}
      </div>
    </div>
  </div>
}
