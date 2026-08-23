import { KeyboardEvent, PointerEvent, useEffect, useRef, useState } from 'react'
import { api, type Artifact, type EditOperation, type EditorState } from './api'

type Tool = 'pencil' | 'brush' | 'eyedropper' | 'flood_fill' | 'lasso' | 'component' | 'protect'

export function FlattenedEditor({ projectId, revisionId, flattened, onCommit }: { projectId: string; revisionId: string; flattened: Artifact; onCommit: (id: string) => void }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const [state, setState] = useState<EditorState | null>(null)
  const [tool, setTool] = useState<Tool>('brush')
  const [label, setLabel] = useState(0)
  const [radius, setRadius] = useState(6)
  const [points, setPoints] = useState<[number, number][]>([])
  const [cursor, setCursor] = useState<[number, number]>([0, 0])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    api.editor(revisionId, controller.signal).then(next => { setState(next); setCursor([Math.floor(next.width / 2), Math.floor(next.height / 2)]); setLabel(value => Math.min(value, next.palette.length - 1)) }).catch(cause => { if (!controller.signal.aborted) setError(String(cause)) })
    return () => controller.abort()
  }, [revisionId])
  useEffect(() => {
    if (!state || !canvas.current) return
    const image = new Image()
    image.onload = () => { const target = canvas.current!; target.width = state.width; target.height = state.height; target.getContext('2d')!.drawImage(image, 0, 0) }
    image.src = flattened.download_url
  }, [state, flattened.id, flattened.download_url])

  function point(event: PointerEvent<HTMLCanvasElement>): [number, number] {
    const bounds = event.currentTarget.getBoundingClientRect()
    return [Math.max(0, Math.min(event.currentTarget.width - 1, Math.floor((event.clientX - bounds.left) * event.currentTarget.width / bounds.width))), Math.max(0, Math.min(event.currentTarget.height - 1, Math.floor((event.clientY - bounds.top) * event.currentTarget.height / bounds.height)))]
  }
  async function commit(operation: EditOperation) {
    setBusy(true); setError('')
    try { onCommit((await api.applyEdits(projectId, revisionId, [operation])).id) }
    catch (cause) { setError(String(cause)) }
    finally { setBusy(false); setPoints([]) }
  }
  function down(event: PointerEvent<HTMLCanvasElement>) {
    event.currentTarget.setPointerCapture(event.pointerId)
    const here = point(event)
    if (tool === 'eyedropper' && state) {
      const pixel = canvas.current!.getContext('2d')!.getImageData(here[0], here[1], 1, 1).data
      const found = state.palette.findIndex(row => row.rgb.every((value, index) => value === pixel[index]))
      if (found >= 0) setLabel(found)
    } else if (['flood_fill', 'component', 'protect'].includes(tool)) {
      void commit({ tool: tool as EditOperation['tool'], label, points: [here] })
    } else setPoints([here])
  }
  function move(event: PointerEvent<HTMLCanvasElement>) {
    if (points.length && ['pencil', 'brush', 'lasso'].includes(tool)) {
      const here = point(event)
      setPoints(value => [...value, here])
    }
  }
  function up() {
    if (!points.length) return
    if (tool === 'lasso' && points.length < 3) return setPoints([])
    void commit({ tool: tool as EditOperation['tool'], label, radius: tool === 'pencil' ? 0 : radius, points })
  }
  function sample(here: [number, number]) {
    if (!state || !canvas.current) return
    const pixel = canvas.current.getContext('2d')!.getImageData(here[0], here[1], 1, 1).data
    const found = state.palette.findIndex(row => row.rgb.every((value, index) => value === pixel[index]))
    if (found >= 0) setLabel(found)
  }
  function applyAtCursor() {
    if (tool === 'eyedropper') sample(cursor)
    else if (tool === 'lasso') setPoints(value => [...value, cursor])
    else void commit({ tool: tool as EditOperation['tool'], label, radius: tool === 'pencil' ? 0 : radius, points: [cursor] })
  }
  function keyDown(event: KeyboardEvent<HTMLCanvasElement>) {
    const directions: Partial<Record<string, [number, number]>> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }
    const direction = directions[event.key]
    if (direction && state) {
      event.preventDefault()
      const step = event.shiftKey ? 10 : 1
      setCursor(([x, y]) => [Math.max(0, Math.min(state.width - 1, x + direction[0] * step)), Math.max(0, Math.min(state.height - 1, y + direction[1] * step))])
    } else if (event.key === ' ') {
      event.preventDefault(); if (!busy) applyAtCursor()
    } else if (event.key === 'Enter' && tool === 'lasso') {
      event.preventDefault()
      if (!busy && points.length >= 3) void commit({ tool: 'lasso', label, radius, points })
    } else if (event.key === 'Escape' && points.length) {
      event.preventDefault(); setPoints([])
    }
  }
  async function paletteEdit(operation: EditOperation) { await commit(operation) }

  if (!state) return <section className="flattened-editor"><h2>Flattened editor</h2><p role={error ? 'alert' : 'status'}>{error || 'Loading editor…'}</p></section>
  return <section className="flattened-editor">
    <div className="editor-head"><div><h2>Flattened editor</h2><p>Every gesture commits an immutable label-map revision.</p></div><span>{state.width}×{state.height} · {state.protected_pixels} protected px</span></div>
    <div className="editor-layout">
      <div><p id="flattened-keyboard-help">Keyboard: arrows move the cursor (Shift moves 10 pixels); Space applies the selected tool. For lasso, Space adds a vertex, Enter commits three or more vertices, and Escape cancels.</p><canvas ref={canvas} tabIndex={0} aria-label="Editable flattened label map" aria-describedby="flattened-keyboard-help flattened-cursor-status" aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight Shift+ArrowUp Shift+ArrowDown Shift+ArrowLeft Shift+ArrowRight Space Enter Escape" onKeyDown={keyDown} onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={() => setPoints([])} /><p id="flattened-cursor-status" role="status" aria-live="polite">Keyboard cursor: x {cursor[0]}, y {cursor[1]}{tool === 'lasso' ? ` · ${points.length} vertices` : ''}</p>{error && <p className="error" role="alert">{error}</p>}</div>
      <div className="editor-controls">
        <div className="tool-grid">{(['pencil', 'brush', 'eyedropper', 'flood_fill', 'lasso', 'component', 'protect'] as Tool[]).map(value => <button key={value} style={{ minHeight: 44 }} disabled={busy} className={tool === value ? 'active' : ''} onClick={() => setTool(value)}>{value.replace('_', ' ')}</button>)}</div>
        <label>Brush radius · {radius}px<input type="range" min="1" max="64" value={radius} onChange={event => setRadius(Number(event.target.value))} /></label>
        <div className="palette-list">{state.palette.map(colour => <div key={colour.index} className={label === colour.index ? 'chosen' : ''}>
          <button className="swatch" aria-label={`Select ${colour.name}`} style={{ background: colour.hex, minHeight: 44 }} onClick={() => setLabel(colour.index)} />
          <input aria-label={`Name for ${colour.hex}`} value={colour.name} onChange={event => setState(current => current && ({ ...current, palette: current.palette.map(row => row.index === colour.index ? { ...row, name: event.target.value } : row) }))} onBlur={event => event.target.value.trim() && paletteEdit({ tool: 'rename_colour', label: colour.index, name: event.target.value.trim() })} />
          <input aria-label={`Colour for ${colour.name}`} type="color" value={colour.hex} onChange={event => { const hex = event.target.value; void paletteEdit({ tool: 'adjust_colour', label: colour.index, rgb: [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16)) as [number, number, number] }) }} />
          {state.palette.length > 1 && colour.index !== label && <button className="merge" onClick={() => paletteEdit({ tool: 'merge_colour', source_label: colour.index, target_label: label })}>Merge →</button>}
        </div>)}</div>
      </div>
    </div>
  </section>
}
