// @vitest-environment jsdom
import { Profiler, type ReactNode } from 'react'
import { act, cleanup, fireEvent, render, renderHook, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'
import { NODE_MARKER_COLOURS, NODE_MARKER_RADIUS, useTracedNodes, committedLayer, drawNodeMarkers, forEachNodeIn, indexNodes, parseTracedNodes, subpathNodes, type NodeLayer } from './PathNodes'

const SODIPODI = 'http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd'
const svgDocument = (body: string, viewBox = '0 0 1000 700') =>
  `<?xml version='1.0' encoding='utf-8'?>\n<svg xmlns="http://www.w3.org/2000/svg" xmlns:sodipodi="${SODIPODI}" viewBox="${viewBox}">${body}</svg>`

// Two layers sharing the nodes (300,100) and (300,300), as a shared boundary does.
// Path A returns to its start (the closing segment is degenerate): 3 nodes, 4 types, and
// the start node's type is the repeated last character, as Inkscape reads it. Path B
// closes with a real line: 3 nodes, 4 types.
const COMMITTED = svgDocument(`
  <g id="layer-red"><path id="a" d="M100 100 L300 100 C400 100 400 300 300 300 L100 100 Z" sodipodi:nodetypes="cscz"/></g>
  <g id="layer-blue"><path id="b" d="M300 100 L500 100 L300 300 Z" sodipodi:nodetypes="zccz"/></g>`)

// The calls of the marker pass (the paths that are filled), leaving out the white ring
// drawn around each marker's outline.
function markerPass<T extends { name: string }>(calls: T[]): T[] {
  const passes: T[][] = []
  for (const call of calls) {
    if (call.name === 'beginPath') passes.push([])
    else passes[passes.length - 1]?.push(call)
  }
  return passes.filter(pass => pass.some(call => call.name === 'fill')).flat()
}

describe('path node parsing', () => {
  it('lays out nodes per subpath the way Inkscape counts them', () => {
    expect(subpathNodes('M0 0 L4 0 L4 4 L0 0 Z M1 1 L2 1 L2 2 Z M9 9 L10 10')).toEqual([
      { points: [[0, 0], [4, 0], [4, 4]], closed: true },
      { points: [[1, 1], [2, 1], [2, 2]], closed: true },
      { points: [[9, 9], [10, 10]], closed: false },
    ])
    // Relative and shorthand commands resolve to absolute node positions (after Z the
    // current point is the subpath's start); an empty subpath has no nodes.
    expect(subpathNodes('M5 5 Z m1 1 h2 v2 c0 1 1 1 1 2 q1 1 2 2 z')).toEqual([
      { points: [[6, 6], [8, 6], [8, 8], [9, 10], [11, 12]], closed: true },
    ])
  })

  it('reads each node type, the closed start from its repeated character, and dedupes shared nodes keeping the sharper type', () => {
    const nodes = parseTracedNodes(COMMITTED)
    const byPoint = Object.fromEntries([...nodes.xs].map((x, index) => [`${x},${nodes.ys[index]}`, nodes.types[index]]))
    // 0 cusp, 1 symmetric, 2 smooth.
    expect(byPoint).toEqual({ '100,100': 2, '300,100': 1, '300,300': 0, '500,100': 0 })
    expect(nodes.counts).toEqual({ cusp: 2, symmetric: 1, smooth: 1 })
    expect(nodes.untyped).toBe(0)
    expect(nodes.viewBox).toEqual({ x: 0, y: 0, width: 1000, height: 700 })
  })

  it('applies ancestor translations and counts nodes without a recorded type', () => {
    const nodes = parseTracedNodes(svgDocument(`<g transform="translate(10 20)"><path d="M0 0 L5 0 L0 5 Z" sodipodi:nodetypes="ccc"/></g>`))
    expect([...nodes.xs]).toEqual([15, 10])
    expect([...nodes.ys]).toEqual([20, 25])
    // The start node's type is the missing repeated fourth character.
    expect(nodes.untyped).toBe(1)
    const bare = parseTracedNodes(svgDocument('<path d="M0 0 L5 0 L0 5 Z"/>'))
    expect(bare.xs.length).toBe(0)
    expect(bare.untyped).toBe(3)
  })
})

describe('node culling and drawing', () => {
  function recorder() {
    const calls: { name: string; args: unknown[] }[] = []
    const log = (name: string) => (...args: unknown[]) => { calls.push({ name, args }) }
    const context = {
      calls, fillStyle: '', strokeStyle: '', lineWidth: 1, lineJoin: 'miter' as CanvasLineJoin,
      setTransform: log('setTransform'), clearRect: (...args: unknown[]) => { calls.length = 0; calls.push({ name: 'clearRect', args }) },
      beginPath: log('beginPath'), moveTo: log('moveTo'), lineTo: log('lineTo'), closePath: log('closePath'),
      rect: log('rect'), arc: log('arc'), fill: log('fill'),
      stroke: () => { calls.push({ name: 'stroke', args: [context.strokeStyle] }) },
    }
    return context
  }
  const index = indexNodes(parseTracedNodes(COMMITTED))
  const layer = committedLayer(index, { width: 1000, height: 700 })!

  it('visits only the nodes inside a rectangle', () => {
    const seen: number[] = []
    forEachNodeIn(index, { x0: 250, y0: 50, x1: 550, y1: 150 }, node => seen.push(node))
    expect(seen.map(node => [index.nodes.xs[node], index.nodes.ys[node]]).sort()).toEqual([[300, 100], [500, 100]])
  })

  it('draws a rhombus for a cusp, a square for a symmetric node and a circle for a smooth node, at a constant screen size', () => {
    for (const scale of [1, 4]) {
      const context = recorder()
      const { drawn } = drawNodeMarkers(context, [layer], { x: 0, y: 0, scale }, { width: 4000, height: 3000 }, 1)
      expect(drawn).toBe(4)
      const rects = markerPass(context.calls).filter(call => call.name === 'rect')
      const arcs = markerPass(context.calls).filter(call => call.name === 'arc')
      // The symmetric node at (300,100) is the square; the smooth node at (100,100) the circle.
      expect(rects).toEqual([{ name: 'rect', args: [300 * scale - NODE_MARKER_RADIUS * .85, 100 * scale - NODE_MARKER_RADIUS * .85, NODE_MARKER_RADIUS * 1.7, NODE_MARKER_RADIUS * 1.7] }])
      expect(arcs).toEqual([{ name: 'arc', args: [100 * scale, 100 * scale, NODE_MARKER_RADIUS, 0, Math.PI * 2] }])
      // Two cusps, each a closed four-point rhombus.
      expect(markerPass(context.calls).filter(call => call.name === 'closePath')).toHaveLength(2)
      expect(markerPass(context.calls).filter(call => call.name === 'lineTo')).toHaveLength(6)
    }
  })

  it('outlines each type in its colour inside a white ring that touches the outline, both 80% opaque', () => {
    const context = recorder()
    drawNodeMarkers(context, [layer], { x: 0, y: 0, scale: 1 }, { width: 4000, height: 3000 }, 1)
    expect(context.calls.filter(call => call.name === 'stroke').map(call => call.args[0])).toEqual(
      ['#ffffffcc', `${NODE_MARKER_COLOURS.cusp}cc`, '#ffffffcc', `${NODE_MARKER_COLOURS.symmetric}cc`, '#ffffffcc', `${NODE_MARKER_COLOURS.smooth}cc`])
    expect(context.fillStyle).toBe('rgba(255, 255, 255, .1)')
    // The ring's centreline sits one outline width outside the marker's, so the two strokes abut.
    const line = context.lineWidth
    const arcs = context.calls.filter(call => call.name === 'arc')
    expect(arcs.map(call => call.args[2])).toEqual([NODE_MARKER_RADIUS + line, NODE_MARKER_RADIUS])
    const squares = context.calls.filter(call => call.name === 'rect')
    expect(squares.map(call => call.args[2])).toEqual([2 * (NODE_MARKER_RADIUS * .85 + line), NODE_MARKER_RADIUS * 1.7])
  })

  it('culls nodes outside the viewport and honours clip and exclude rectangles', () => {
    // Panned so only x in [400, 1200] is in view: just (500,100).
    expect(drawNodeMarkers(recorder(), [layer], { x: -400, y: 0, scale: 1 }, { width: 800, height: 600 }, 1).drawn).toBe(1)
    const clipped: NodeLayer = { ...layer, clip: { x0: 0, y0: 0, x1: 200, y1: 200 } }
    expect(drawNodeMarkers(recorder(), [clipped], { x: 0, y: 0, scale: 1 }, { width: 1000, height: 700 }, 1).drawn).toBe(1)
    const excluded: NodeLayer = { ...layer, exclude: { x0: 0, y0: 0, x1: 200, y1: 200 } }
    expect(drawNodeMarkers(recorder(), [excluded], { x: 0, y: 0, scale: 1 }, { width: 1000, height: 700 }, 1).drawn).toBe(3)
  })

  it('counts by type only the nodes inside the viewport, not those drawn only for their marker\'s reach', () => {
    // Unpanned, all four nodes are in view.
    expect(drawNodeMarkers(recorder(), [layer], { x: 0, y: 0, scale: 1 }, { width: 800, height: 600 }, 1).inView).toEqual({ cusp: 2, symmetric: 1, smooth: 1 })
    // Panned so the view starts at x 302: (300,100) and (300,300) sit 2px off-screen, so
    // their markers reach in and are drawn, but only (500,100) is in view.
    const edge = drawNodeMarkers(recorder(), [layer], { x: -302, y: 0, scale: 1 }, { width: 800, height: 600 }, 1)
    expect(edge.drawn).toBe(3)
    expect(edge.inView).toEqual({ cusp: 1, symmetric: 0, smooth: 0 })
    // Panned further, nothing is in view.
    expect(drawNodeMarkers(recorder(), [layer], { x: -600, y: 0, scale: 1 }, { width: 800, height: 600 }, 1).inView).toEqual({ cusp: 0, symmetric: 0, smooth: 0 })
  })

  it('counts the preview\'s nodes inside its window and the committed trace\'s outside it, not the preview\'s margin', () => {
    // The preview window is stage (150,150)-(450,450). The preview's cusp at (200,200) is in
    // it; its symmetric nodes at (50..70, 50..70) lie in its margin, in view but clipped.
    const preview = indexNodes(parseTracedNodes(svgDocument(
      '<path d="M200 200 L260 200 L200 260 Z" sodipodi:nodetypes="cccc"/><path d="M50 50 L70 50 L50 70 Z" sodipodi:nodetypes="ssss"/>')))
    const window = { x0: 150, y0: 150, x1: 450, y1: 450 }
    const layers: NodeLayer[] = [
      { ...layer, exclude: window },
      { index: preview, scaleX: 1, scaleY: 1, offsetX: 0, offsetY: 0, clip: window },
    ]
    // Committed: (100,100) smooth, (300,100) symmetric, (500,100) cusp; (300,300) is in the
    // window, so the preview's three cusps stand there instead.
    expect(drawNodeMarkers(recorder(), layers, { x: 0, y: 0, scale: 1 }, { width: 800, height: 600 }, 1).inView).toEqual({ cusp: 4, symmetric: 1, smooth: 1 })
    // Viewed from x 230, only part of the window is in view: the preview's (260,200) and
    // the committed (300,100) and (500,100).
    expect(drawNodeMarkers(recorder(), layers, { x: -230, y: 0, scale: 1 }, { width: 800, height: 600 }, 1).inView).toEqual({ cusp: 2, symmetric: 1, smooth: 0 })
  })

  it('keeps a node on the preview window\'s edge exactly once at a stage scale of 1/3', () => {
    // A 2100px traced grid on a 700px stage. The window's right edge is at traced x 7,
    // stage 7/3; the nodes on it are the preview's (symmetric), the committed trace's
    // there (cusps) are excluded.
    const shape = (code: string) => svgDocument(`<path d="M2 10 L7 10 L7 20 Z" sodipodi:nodetypes="${code.repeat(4)}"/>`, '0 0 2100 2100')
    const committed = committedLayer(indexNodes(parseTracedNodes(shape('c'))), { width: 700, height: 700 })!
    const k = 700 / 2100
    const window = { x0: 0, y0: 0, x1: 7 * k, y1: 1000 * k }
    const layers: NodeLayer[] = [
      { ...committed, exclude: window },
      { index: indexNodes(parseTracedNodes(shape('s'))), scaleX: k, scaleY: k, offsetX: 0, offsetY: 0, clip: window },
    ]
    const result = drawNodeMarkers(recorder(), layers, { x: 0, y: 0, scale: 1 }, { width: 800, height: 600 }, 1)
    expect(result.drawn).toBe(3)
    expect(result.inView).toEqual({ cusp: 0, symmetric: 3, smooth: 0 })
  })

  it('places nodes only when the SVG grid registers with the stage', () => {
    expect(committedLayer(index, { width: 2000, height: 1400 })).toMatchObject({ scaleX: 2, scaleY: 2 })
    expect(committedLayer(index, { width: 1000, height: 800 })).toBeNull()
  })

  it('stays cheap with tens of thousands of nodes', () => {
    const paths: string[] = []
    for (let row = 0; row < 100; row += 1) {
      const points = Array.from({ length: 100 }, (_, column) => `L${column * 10 + 5} ${row * 7 + 3}`).join(' ')
      paths.push(`<path d="M0 ${row * 7} ${points} Z" sodipodi:nodetypes="${'c'.repeat(102)}"/>`)
    }
    const started = performance.now()
    const big = indexNodes(parseTracedNodes(svgDocument(paths.join(''))))
    expect(big.nodes.xs.length).toBe(10100)
    // Zoomed in on a corner, a draw visits only that corner's nodes.
    expect(drawNodeMarkers(recorder(), [committedLayer(big, { width: 1000, height: 700 })!], { x: 0, y: 0, scale: 8 }, { width: 400, height: 280 }, 1).drawn).toBeLessThan(100)
    expect(performance.now() - started).toBeLessThan(5000)
  })
})

describe('Trace canvas Nodes overlay', () => {
  const layers: WorkspaceLayer[] = [
    { id: 'svg', name: 'Traced SVG', url: '/traced.svg', mediaType: 'image/svg+xml', role: 'image' },
    { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
  ]
  type Pending = { url: string; init?: RequestInit; resolve: (response: unknown) => void }
  let pending: Pending[] = []
  let drawn: { name: string; args: number[] }[] = []

  beforeEach(() => {
    pending = []
    drawn = []
    localStorage.clear()
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => new Promise(resolve => pending.push({ url, init, resolve }))))
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1 })
    vi.stubGlobal('cancelAnimationFrame', () => undefined)
    const log = (name: string) => (...args: number[]) => { drawn.push({ name, args }) }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ({
      setTransform: log('setTransform'), clearRect: () => { drawn = [] }, beginPath: log('beginPath'), moveTo: log('moveTo'),
      lineTo: log('lineTo'), closePath: log('closePath'), rect: log('rect'), arc: log('arc'), fill: log('fill'), stroke: log('stroke'),
    }) as unknown as CanvasRenderingContext2D)
    Object.defineProperty(HTMLCanvasElement.prototype, 'clientWidth', { configurable: true, get: () => 800 })
    Object.defineProperty(HTMLCanvasElement.prototype, 'clientHeight', { configurable: true, get: () => 600 })
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    delete (HTMLCanvasElement.prototype as { clientWidth?: number }).clientWidth
    delete (HTMLCanvasElement.prototype as { clientHeight?: number }).clientHeight
    localStorage.clear()
  })

  type Props = Partial<Parameters<typeof ImageWorkspace>[0]>
  function prepare(props: Props = {}, wrapper?: (props: { children: ReactNode }) => ReactNode) {
    const result = render(<ImageWorkspace layers={layers} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" pathNodes={{ url: '/traced.svg' }} {...props} />, { wrapper })
    const viewport = within(result.container).getByLabelText('Image comparison viewport')
    Object.defineProperties(viewport, { clientWidth: { configurable: true, value: 800 }, clientHeight: { configurable: true, value: 600 } })
    viewport.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) })
    for (const image of result.container.querySelectorAll('img')) {
      Object.defineProperties(image, { naturalWidth: { configurable: true, value: 1000 }, naturalHeight: { configurable: true, value: 700 } })
      fireEvent.load(image)
    }
    return result
  }
  const nodesSwitch = () => within(screen.getByRole('group', { name: 'Inspection layers' })).getByLabelText('Nodes') as HTMLInputElement
  async function serveCommitted() {
    const request = pending.find(entry => entry.url === '/traced.svg')!
    await act(async () => { request.resolve({ ok: true, status: 200, text: async () => COMMITTED }) })
  }
  const markers = () => markerPass(drawn).filter(call => call.name === 'rect' || call.name === 'arc' || call.name === 'closePath').length

  it('offers the Nodes switch only on a canvas given a traced SVG, and fetches it only once switched on', () => {
    prepare()
    expect(nodesSwitch().checked).toBe(false)
    expect(pending).toHaveLength(0)
    expect(screen.queryByRole('list', { name: 'Node types' })).toBeNull()
    cleanup()
    prepare({ pathNodes: undefined })
    expect(screen.queryByRole('group', { name: 'Inspection layers' })).toBeNull()
  })

  it('draws every node with its shape and a legend of the three types with their counts, and clears on Off', async () => {
    const { container } = prepare()
    fireEvent.click(nodesSwitch())
    expect(pending.map(entry => entry.url)).toEqual(['/traced.svg'])
    expect(screen.getByText('Loading nodes…')).toBeTruthy()
    await serveCommitted()
    const legend = screen.getByRole('list', { name: 'Node types' })
    expect([...legend.querySelectorAll('li')].map(item => item.textContent)).toEqual(['Cusp node2', 'Symmetric node1', 'Smooth node1'])
    expect(screen.getByText('Nodes in view').id).toBe(legend.getAttribute('aria-describedby'))
    expect([...legend.querySelectorAll('svg')].map(shape => shape.firstElementChild?.tagName)).toEqual(['polygon', 'rect', 'circle'])
    expect(container.querySelector('canvas.node-overlay')).toBeTruthy()
    expect(markers()).toBe(4)
    // A zoom redraws at the same marker size.
    fireEvent.click(screen.getByLabelText('Zoom in'))
    expect(markerPass(drawn).find(call => call.name === 'arc')?.args[2]).toBe(NODE_MARKER_RADIUS)
    fireEvent.click(nodesSwitch())
    expect(container.querySelector('canvas.node-overlay')).toBeNull()
    expect(screen.queryByRole('list', { name: 'Node types' })).toBeNull()
    // Back on, the parsed trace is reused.
    fireEvent.click(nodesSwitch())
    expect(pending).toHaveLength(1)
    expect(container.querySelector('canvas.node-overlay')).toBeTruthy()
  })

  function dragRight(distance: number) {
    const viewport = screen.getByLabelText('Image comparison viewport')
    viewport.setPointerCapture = () => undefined
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 100 + distance, clientY: 100 })
    fireEvent.pointerUp(viewport, { pointerId: 1 })
  }
  const counts = () => [...screen.getByRole('list', { name: 'Node types' }).querySelectorAll('.node-count')].map(count => Number(count.textContent))

  it('counts only the nodes in view, updating on zoom and pan', async () => {
    prepare()
    fireEvent.click(nodesSwitch())
    await serveCommitted()
    expect(counts()).toEqual([2, 1, 1])
    // Two 1.25x zooms about the centre: scale 1.2, origin (-200, -120). The smooth node at
    // (100,100) is now off-screen at x -80.
    fireEvent.click(screen.getByLabelText('Zoom in'))
    fireEvent.click(screen.getByLabelText('Zoom in'))
    expect(counts()).toEqual([2, 1, 0])
    // Dragged 150px right, it is back in view.
    dragRight(150)
    expect(counts()).toEqual([2, 1, 1])
    // Dragged until no node is in view, the counts read 0.
    dragRight(-1400)
    expect(counts()).toEqual([0, 0, 0])
    expect(screen.getByText('Nodes in view')).toBeTruthy()
  })

  it('sets no state on a draw whose counts are unchanged, so a pan move renders once', async () => {
    let commits = 0
    prepare({}, ({ children }) => <Profiler id="workspace" onRender={() => { commits += 1 }}>{children}</Profiler>)
    fireEvent.click(nodesSwitch())
    await serveCommitted()
    fireEvent.click(screen.getByLabelText('Zoom in'))
    fireEvent.click(screen.getByLabelText('Zoom in'))
    expect(counts()).toEqual([2, 1, 0])
    const viewport = screen.getByLabelText('Image comparison viewport')
    viewport.setPointerCapture = () => undefined
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 100, clientY: 100 })
    commits = 0
    // Five 5px moves keep the same nodes in view: one commit each, none for the counts.
    for (let step = 1; step <= 5; step += 1) fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 100 + 5 * step, clientY: 100 })
    expect(commits).toBe(5)
    expect(counts()).toEqual([2, 1, 0])
  })

  it('never shows counts from another drawing: after Off and on, comparing, or a new trace', async () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback))
    vi.stubGlobal('cancelAnimationFrame', (id: number) => { frames[id - 1] = () => undefined })
    const flush = () => act(() => { frames.splice(0).forEach(callback => callback(0)) })
    const shown = () => screen.getByRole('list', { name: 'Node types' }).querySelectorAll('.node-count').length
    const { rerender } = prepare({ compareBefore: ['flat'] })
    fireEvent.click(nodesSwitch())
    await serveCommitted()
    flush()
    expect(counts()).toEqual([2, 1, 1])
    // Off, zoomed, and on again: nothing until the new view is drawn and counted.
    fireEvent.click(nodesSwitch())
    fireEvent.click(screen.getByLabelText('Zoom in'))
    fireEvent.click(screen.getByLabelText('Zoom in'))
    fireEvent.click(nodesSwitch())
    expect(shown()).toBe(0)
    flush()
    expect(counts()).toEqual([2, 1, 0])
    // Comparing hides them; Off again waits for a draw.
    const compare = () => screen.getByRole('group', { name: 'Compare' })
    fireEvent.click(within(compare()).getByRole('button', { name: 'split' }))
    expect(shown()).toBe(0)
    fireEvent.click(within(compare()).getByRole('button', { name: 'Off' }))
    expect(shown()).toBe(0)
    flush()
    expect(counts()).toEqual([2, 1, 0])
    // A new revision's trace: none while it loads, nor once loaded until it is drawn.
    rerender(<ImageWorkspace layers={layers} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" pathNodes={{ url: '/traced-2.svg' }} compareBefore={['flat']} />)
    expect(shown()).toBe(0)
    const next = pending.find(entry => entry.url === '/traced-2.svg')!
    await act(async () => { next.resolve({ ok: true, status: 200, text: async () => svgDocument('<path d="M400 400 L420 400 L400 420 Z" sodipodi:nodetypes="ssss"/>') }) })
    expect(shown()).toBe(0)
    flush()
    expect(counts()).toEqual([0, 3, 0])
  })



  it('says when the trace records no node types', async () => {
    prepare()
    fireEvent.click(nodesSwitch())
    const request = pending[0]
    await act(async () => { request.resolve({ ok: true, status: 200, text: async () => svgDocument('<path d="M0 0 L5 0 L0 5 Z"/>') }) })
    expect(screen.getByText('This trace has no node types. Re-run Trace to record them.')).toBeTruthy()
    // With no nodes to draw, no counts or caption are shown.
    expect(screen.getByRole('list', { name: 'Node types' }).querySelector('.node-count')).toBeNull()
    expect(screen.queryByText('Nodes in view')).toBeNull()
  })

  it('hides the nodes while comparing, keeping the switch on', async () => {
    const { container } = prepare({ compareBefore: ['flat'] })
    fireEvent.click(nodesSwitch())
    await serveCommitted()
    const compare = () => screen.getByRole('group', { name: 'Compare' })
    for (const mode of ['split', 'blink']) {
      fireEvent.click(within(compare()).getByRole('button', { name: mode }))
      expect(container.querySelector('canvas.node-overlay')).toBeNull()
      // Nothing is drawn, so no counts are shown.
      expect(container.querySelector('.node-count')).toBeNull()
      expect(screen.queryByText('Nodes in view')).toBeNull()
      expect(within(compare()).getByText('Overlays and live preview hidden while comparing')).toBeTruthy()
      expect(nodesSwitch().checked).toBe(true)
    }
    fireEvent.click(within(compare()).getByRole('button', { name: 'Off' }))
    expect(container.querySelector('canvas.node-overlay')).toBeTruthy()
    expect(markers()).toBe(4)
    expect(counts()).toEqual([2, 1, 1])
  })
})

describe('useTracedNodes', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('fetches again after the switch is turned off mid-load, and reuses a settled result', async () => {
    const requests: { url: string; signal?: AbortSignal; resolve: (response: unknown) => void }[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => new Promise((resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      requests.push({ url, signal: init?.signal ?? undefined, resolve })
    })))
    const { result, rerender } = renderHook(({ url }: { url: string | null }) => useTracedNodes(url), { initialProps: { url: '/traced.svg' as string | null } })
    expect(result.current.status).toBe('loading')
    expect(requests).toHaveLength(1)
    // Off before the SVG arrives: the fetch is abandoned.
    await act(async () => { rerender({ url: null }) })
    expect(requests[0].signal?.aborted).toBe(true)
    expect(result.current.status).toBe('idle')
    // On again: a new fetch, which settles.
    await act(async () => { rerender({ url: '/traced.svg' }) })
    expect(requests).toHaveLength(2)
    await act(async () => { requests[1].resolve({ ok: true, status: 200, text: async () => COMMITTED }) })
    expect(result.current.status).toBe('ready')
    // Off and on once settled: the parsed result is reused, nothing is fetched.
    await act(async () => { rerender({ url: null }) })
    await act(async () => { rerender({ url: '/traced.svg' }) })
    expect(requests).toHaveLength(2)
    expect(result.current.status).toBe('ready')
  })
})
