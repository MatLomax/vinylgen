import { useEffect, useMemo, useState } from 'react'
import { gridsRegister, type ImageSize, type ViewTransform } from './workspace'

// The Trace canvas's "Nodes" overlay: every path node of a traced SVG, marked with its
// type's shape (GLOSSARY.md "Path nodes"). The types are read from the SVG's own
// `sodipodi:nodetypes`, the one classification the tracer writes and Inkscape reads.

export const NODE_TYPES = ['cusp', 'symmetric', 'smooth'] as const
export type NodeType = typeof NODE_TYPES[number]
// `sodipodi:nodetypes` characters: Inkscape's `s` ("smooth") is Vinylgen's symmetric
// node and its `z` ("symmetric") is Vinylgen's smooth node.
const TYPE_CODES: Record<string, number> = { c: 0, s: 1, z: 2 }
export const NODE_TYPE_LABELS: Record<NodeType, string> = { cusp: 'Cusp node', symmetric: 'Symmetric node', smooth: 'Smooth node' }
// Half the on-screen marker size, in CSS px; markers keep this size at every zoom.
export const NODE_MARKER_RADIUS = 4.5

// Deduplicated nodes in the SVG's user units; `types` holds indexes into NODE_TYPES.
// `untyped` counts the nodes of paths without (or with a short) `sodipodi:nodetypes`;
// they are not drawn, since their type is unknown.
export type ParsedNodes = {
  viewBox: { x: number; y: number; width: number; height: number }
  xs: Float64Array; ys: Float64Array; types: Uint8Array
  counts: Record<NodeType, number>; untyped: number
}

type Affine = [number, number, number, number, number, number]
const IDENTITY: Affine = [1, 0, 0, 1, 0, 0]
const multiply = (a: Affine, b: Affine): Affine => [
  a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1],
  a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
  a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5],
]

function parseTransform(value: string | null): Affine {
  let result = IDENTITY
  if (!value) return result
  for (const [, name, body] of value.matchAll(/(\w+)\s*\(([^)]*)\)/g)) {
    const n = (body.match(/[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?/g) ?? []).map(Number)
    const step: Affine = name === 'translate' ? [1, 0, 0, 1, n[0] ?? 0, n[1] ?? 0]
      : name === 'scale' ? [n[0] ?? 1, 0, 0, n[1] ?? n[0] ?? 1, 0, 0]
        : name === 'matrix' && n.length === 6 ? n as Affine
          : (() => { throw new Error(`unsupported SVG transform ${name}`) })()
    result = multiply(result, step)
  }
  return result
}

const PARAMETERS: Record<string, number> = { M: 2, L: 2, H: 1, V: 1, C: 6, S: 4, Q: 4, T: 2, A: 7, Z: 0 }

/**
 * The node positions of each subpath of path data `d`, laid out as Inkscape counts
 * them: the start point and every segment's end, except that a closed subpath's final
 * node is its start node (the closing segment ends there). A subpath without segments
 * has no nodes.
 */
export function subpathNodes(d: string): { points: [number, number][]; closed: boolean }[] {
  const tokens = d.match(/[MLHVCSQTAZmlhvcsqtaz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?/g) ?? []
  const subpaths: { points: [number, number][]; closed: boolean }[] = []
  let points: [number, number][] | null = null
  let x = 0, y = 0, startX = 0, startY = 0
  let command = ''
  let index = 0
  const finish = (closed: boolean) => {
    if (!points) return
    if (points.length > 1) {
      if (closed) {
        // The last node sits on the start when the path returns there; otherwise the
        // closing line ends on the start. Either way the start node stands for it.
        const [lx, ly] = points[points.length - 1]
        if (lx === points[0][0] && ly === points[0][1]) points.pop()
      }
      subpaths.push({ points, closed })
    }
    points = null
  }
  while (index < tokens.length) {
    const token = tokens[index]
    if (/[a-zA-Z]/.test(token)) {
      command = token
      index += 1
      if (command === 'Z' || command === 'z') {
        finish(true)
        x = startX; y = startY
      }
      continue
    }
    const upper = command.toUpperCase()
    const count = PARAMETERS[upper]
    if (!count) throw new Error(`path data has coordinates without a command: ${d.slice(0, 60)}`)
    const values = tokens.slice(index, index + count).map(Number)
    if (values.length < count || values.some(Number.isNaN)) throw new Error(`truncated path data: ${d.slice(0, 60)}`)
    index += count
    const relative = command !== upper
    let nx: number, ny: number
    if (upper === 'H') { nx = (relative ? x : 0) + values[0]; ny = y }
    else if (upper === 'V') { nx = x; ny = (relative ? y : 0) + values[0] }
    else { nx = (relative ? x : 0) + values[count - 2]; ny = (relative ? y : 0) + values[count - 1] }
    if (upper === 'M') {
      finish(false)
      points = [[nx, ny]]
      startX = nx; startY = ny
      command = relative ? 'l' : 'L'
    } else {
      if (!points) points = [[x, y]]
      points.push([nx, ny])
    }
    x = nx; y = ny
  }
  finish(false)
  return subpaths
}

/**
 * Every node of an SVG document's paths with its type from `sodipodi:nodetypes`, in the
 * document's user units (ancestor transforms applied). A node shared by several paths
 * (a boundary both of its colours' layers draw) is kept once; where its paths disagree
 * the sharper type wins (cusp, then symmetric, then smooth), so no kink is hidden.
 */
export function parseTracedNodes(svgText: string): ParsedNodes {
  const document = new DOMParser().parseFromString(svgText, 'image/svg+xml')
  const root = document.documentElement
  if (root.nodeName !== 'svg') throw new Error('the traced file is not an SVG document')
  const box = (root.getAttribute('viewBox') ?? '').trim().split(/[\s,]+/).map(Number)
  const viewBox = box.length === 4 && box.every(Number.isFinite)
    ? { x: box[0], y: box[1], width: box[2], height: box[3] }
    : { x: 0, y: 0, width: Number(root.getAttribute('width')) || 0, height: Number(root.getAttribute('height')) || 0 }
  const seen = new Map<string, number>()
  const xs: number[] = [], ys: number[] = [], types: number[] = []
  let untyped = 0
  for (const path of document.getElementsByTagNameNS('http://www.w3.org/2000/svg', 'path')) {
    let matrix = IDENTITY
    for (let node: Element | null = path; node && node !== root; node = node.parentElement) matrix = multiply(parseTransform(node.getAttribute('transform')), matrix)
    const codes = path.getAttributeNS('http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd', 'nodetypes') ?? ''
    let cursor = 0
    for (const { points, closed } of subpathNodes(path.getAttribute('d') ?? '')) {
      // A closed subpath's type string repeats its start node's type at the end, and
      // that repeated character is the one Inkscape reads for the start node.
      const chars = codes.slice(cursor, cursor + points.length + (closed ? 1 : 0))
      cursor += points.length + (closed ? 1 : 0)
      points.forEach(([px, py], position) => {
        const code = TYPE_CODES[closed && position === 0 ? chars[points.length] ?? '' : chars[position] ?? '']
        if (code === undefined) { untyped += 1; return }
        const sx = matrix[0] * px + matrix[2] * py + matrix[4]
        const sy = matrix[1] * px + matrix[3] * py + matrix[5]
        const key = `${sx} ${sy}`
        const found = seen.get(key)
        if (found === undefined) {
          seen.set(key, xs.length)
          xs.push(sx); ys.push(sy); types.push(code)
        } else if (code < types[found]) types[found] = code
      })
    }
  }
  const counts = { cusp: 0, symmetric: 0, smooth: 0 }
  for (const code of types) counts[NODE_TYPES[code]] += 1
  return { viewBox, xs: Float64Array.from(xs), ys: Float64Array.from(ys), types: Uint8Array.from(types), counts, untyped }
}

// A uniform grid over the nodes' bounding box, so a redraw visits only the cells in view.
export type NodeIndex = {
  nodes: ParsedNodes; minX: number; minY: number; cell: number; columns: number; rows: number
  starts: Uint32Array; order: Uint32Array
}
const INDEX_CELLS = 128

export function indexNodes(nodes: ParsedNodes): NodeIndex {
  const count = nodes.xs.length
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (let i = 0; i < count; i += 1) {
    minX = Math.min(minX, nodes.xs[i]); maxX = Math.max(maxX, nodes.xs[i])
    minY = Math.min(minY, nodes.ys[i]); maxY = Math.max(maxY, nodes.ys[i])
  }
  if (!count) { minX = minY = 0; maxX = maxY = 1 }
  const cell = Math.max((maxX - minX) / INDEX_CELLS, (maxY - minY) / INDEX_CELLS, 1e-9)
  const columns = Math.floor((maxX - minX) / cell) + 1
  const rows = Math.floor((maxY - minY) / cell) + 1
  const cellOf = new Uint32Array(count)
  const starts = new Uint32Array(columns * rows + 1)
  for (let i = 0; i < count; i += 1) {
    cellOf[i] = Math.floor((nodes.ys[i] - minY) / cell) * columns + Math.floor((nodes.xs[i] - minX) / cell)
    starts[cellOf[i] + 1] += 1
  }
  for (let c = 0; c < columns * rows; c += 1) starts[c + 1] += starts[c]
  const fill = starts.slice(0, columns * rows)
  const order = new Uint32Array(count)
  for (let i = 0; i < count; i += 1) order[fill[cellOf[i]]++] = i
  return { nodes, minX, minY, cell, columns, rows, starts, order }
}

export type Rect = { x0: number; y0: number; x1: number; y1: number }

// Calls `visit` for each node inside `rect` (node units, inclusive).
export function forEachNodeIn(index: NodeIndex, rect: Rect, visit: (node: number) => void) {
  const { nodes, minX, minY, cell, columns, rows, starts, order } = index
  const c0 = Math.max(0, Math.floor((rect.x0 - minX) / cell)), c1 = Math.min(columns - 1, Math.floor((rect.x1 - minX) / cell))
  const r0 = Math.max(0, Math.floor((rect.y0 - minY) / cell)), r1 = Math.min(rows - 1, Math.floor((rect.y1 - minY) / cell))
  for (let r = r0; r <= r1; r += 1) {
    for (let c = c0; c <= c1; c += 1) {
      const at = r * columns + c
      for (let k = starts[at]; k < starts[at + 1]; k += 1) {
        const node = order[k]
        const x = nodes.xs[node], y = nodes.ys[node]
        if (x >= rect.x0 && x <= rect.x1 && y >= rect.y0 && y <= rect.y1) visit(node)
      }
    }
  }
}

// One set of nodes placed on the stage: stage px = node units × scale + offset. Only
// nodes inside `clip` (stage px) are drawn, and none inside `exclude`; both include
// their edges.
export type NodeLayer = { index: NodeIndex; scaleX: number; scaleY: number; offsetX: number; offsetY: number; clip?: Rect; exclude?: Rect }

// Outline width of a marker, in CSS px; the black ring around it is as wide.
const NODE_MARKER_LINE = 1.25
// Each node type's outline colour, on the canvas and in the legend.
export const NODE_MARKER_COLOURS: Record<NodeType, string> = { cusp: '#ff4775', symmetric: '#40b4ff', smooth: '#ffe23d' }
// Opacity of both a marker's coloured outline and its black ring.
export const NODE_MARKER_BORDER_OPACITY = .7
const BORDER_ALPHA_HEX = Math.round(NODE_MARKER_BORDER_OPACITY * 255).toString(16).padStart(2, '0')

// A node type's marker centred on (x, y), grown outward by `grow` CSS px on every side.
function traceMarker(context: Pick<CanvasRenderingContext2D, 'moveTo' | 'lineTo' | 'closePath' | 'rect' | 'arc'>, type: number, x: number, y: number, grow: number) {
  const r = NODE_MARKER_RADIUS
  if (type === 0) {
    const k = r * 1.25 + grow * Math.SQRT2
    context.moveTo(x, y - k); context.lineTo(x + k, y); context.lineTo(x, y + k); context.lineTo(x - k, y); context.closePath()
  } else if (type === 1) {
    const h = r * .85 + grow
    context.rect(x - h, y - h, 2 * h, 2 * h)
  } else {
    context.moveTo(x + r + grow, y); context.arc(x, y, r + grow, 0, Math.PI * 2)
  }
}

type MarkerContext = Pick<CanvasRenderingContext2D, 'setTransform' | 'clearRect' | 'beginPath' | 'moveTo' | 'lineTo' | 'closePath' | 'rect' | 'arc' | 'fill' | 'stroke'> & { fillStyle: unknown; strokeStyle: unknown; lineWidth: number; lineJoin: CanvasLineJoin }

/**
 * Draws every visible node of `layers` as its type's marker at a constant screen size:
 * cusp a rhombus, symmetric a square, smooth a circle. The canvas is viewport-sized at
 * device resolution (`dpr`) and not itself transformed; `transform` places stage px on
 * it exactly as the image stage is placed. Returns how many markers were drawn.
 */
export function drawNodeMarkers(context: MarkerContext, layers: NodeLayer[], transform: ViewTransform, viewport: ImageSize, dpr: number): number {
  context.setTransform(1, 0, 0, 1, 0, 0)
  context.clearRect(0, 0, viewport.width * dpr, viewport.height * dpr)
  if (!transform.scale) return 0
  context.setTransform(dpr, 0, 0, dpr, 0, 0)
  const r = NODE_MARKER_RADIUS
  const margin = (r * 1.25 + 2 * NODE_MARKER_LINE) / transform.scale
  const view: Rect = { x0: -transform.x / transform.scale - margin, y0: -transform.y / transform.scale - margin,
    x1: (viewport.width - transform.x) / transform.scale + margin, y1: (viewport.height - transform.y) / transform.scale + margin }
  const shapes: [number, number][][] = [[], [], []]
  for (const layer of layers) {
    const stage: Rect = layer.clip ? { x0: Math.max(view.x0, layer.clip.x0), y0: Math.max(view.y0, layer.clip.y0), x1: Math.min(view.x1, layer.clip.x1), y1: Math.min(view.y1, layer.clip.y1) } : view
    if (stage.x1 < stage.x0 || stage.y1 < stage.y0) continue
    const ax = (stage.x0 - layer.offsetX) / layer.scaleX, bx = (stage.x1 - layer.offsetX) / layer.scaleX
    const ay = (stage.y0 - layer.offsetY) / layer.scaleY, by = (stage.y1 - layer.offsetY) / layer.scaleY
    const { xs, ys, types } = layer.index.nodes
    const exclude = layer.exclude
    forEachNodeIn(layer.index, { x0: Math.min(ax, bx), y0: Math.min(ay, by), x1: Math.max(ax, bx), y1: Math.max(ay, by) }, node => {
      const sx = xs[node] * layer.scaleX + layer.offsetX, sy = ys[node] * layer.scaleY + layer.offsetY
      if (exclude && sx >= exclude.x0 && sx <= exclude.x1 && sy >= exclude.y0 && sy <= exclude.y1) return
      shapes[types[node]].push([transform.x + sx * transform.scale, transform.y + sy * transform.scale])
    })
  }
  // One path per shape and pass keeps tens of thousands of markers cheap: first the black
  // ring just outside the outline, then the faint fill and the type's coloured outline.
  context.lineWidth = NODE_MARKER_LINE
  context.lineJoin = 'miter'
  context.fillStyle = 'rgba(255, 255, 255, .1)'
  shapes.forEach((points, type) => {
    if (!points.length) return
    context.beginPath()
    for (const [x, y] of points) traceMarker(context, type, x, y, NODE_MARKER_LINE)
    context.strokeStyle = `#000000${BORDER_ALPHA_HEX}`
    context.stroke()
    context.beginPath()
    for (const [x, y] of points) traceMarker(context, type, x, y, 0)
    context.fill()
    context.strokeStyle = `${NODE_MARKER_COLOURS[NODE_TYPES[type]]}${BORDER_ALPHA_HEX}`
    context.stroke()
  })
  return shapes.reduce((total, points) => total + points.length, 0)
}

export type NodeSource = { status: 'idle' } | { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; index: NodeIndex }

/**
 * Fetches and parses the traced SVG at `url` while `url` is set; a new url replaces it.
 * A settled result (ready or error) for the url is kept and reused; a fetch abandoned
 * before it settled (the switch turned off mid-load) is forgotten, so the next request
 * for the url fetches again.
 */
export function useTracedNodes(url: string | null): NodeSource {
  const [state, setState] = useState<{ url: string; source: NodeSource } | null>(null)
  useEffect(() => {
    if (!url || (state?.url === url && (state.source.status === 'ready' || state.source.status === 'error'))) return
    const controller = new AbortController()
    setState({ url, source: { status: 'loading' } })
    fetch(url, { signal: controller.signal })
      .then(response => {
        if (!response.ok) throw new Error(`the traced SVG could not be loaded (${response.status})`)
        return response.text()
      })
      .then(text => setState({ url, source: { status: 'ready', index: indexNodes(parseTracedNodes(text)) } }))
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return
        setState({ url, source: { status: 'error', message: cause instanceof Error ? cause.message : String(cause) } })
      })
    return () => {
      controller.abort()
      setState(current => current?.url === url && current.source.status === 'loading' ? null : current)
    }
  }, [url])
  return url && state?.url === url ? state.source : url ? { status: 'loading' } : { status: 'idle' }
}

/**
 * Parses an SVG document already in hand (the live preview's): its nodes, or `failed`
 * when it could not be read. Both empty without a document.
 */
export function useParsedNodes(svgText: string | null): { index: NodeIndex | null; failed: boolean } {
  return useMemo(() => {
    if (!svgText) return { index: null, failed: false }
    try { return { index: indexNodes(parseTracedNodes(svgText)), failed: false } } catch { return { index: null, failed: true } }
  }, [svgText])
}

// The committed trace's nodes on a stage of `stage` px: the SVG's viewBox is its pixel
// grid, stretched over the stage when the two register. Null when they do not.
export function committedLayer(index: NodeIndex, stage: ImageSize): NodeLayer | null {
  const box = index.nodes.viewBox
  if (!gridsRegister(stage, { width: box.width, height: box.height })) return null
  const scaleX = stage.width / box.width, scaleY = stage.height / box.height
  return { index, scaleX, scaleY, offsetX: -box.x * scaleX, offsetY: -box.y * scaleY }
}

// The marker shape of a node type, drawn in the legend at the canvas markers' size.
export function NodeShape({ type }: { type: NodeType }) {
  const r = NODE_MARKER_RADIUS, d = NODE_MARKER_LINE, k = r * 1.25, size = 2 * (k + d * Math.SQRT2) + 3, c = size / 2
  const shape = (grow: number, style: { fill: string; stroke: string; strokeOpacity: number }) => type === 'cusp'
    ? <polygon points={`${c},${c - k - grow * Math.SQRT2} ${c + k + grow * Math.SQRT2},${c} ${c},${c + k + grow * Math.SQRT2} ${c - k - grow * Math.SQRT2},${c}`} {...style} />
    : type === 'symmetric' ? <rect x={c - r * .85 - grow} y={c - r * .85 - grow} width={2 * (r * .85 + grow)} height={2 * (r * .85 + grow)} {...style} />
      : <circle cx={c} cy={c} r={r + grow} {...style} />
  return <svg className={`node-shape ${type}`} width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
    {shape(d, { fill: 'none', stroke: '#000', strokeOpacity: NODE_MARKER_BORDER_OPACITY })}
    {shape(0, { fill: 'rgba(255, 255, 255, .1)', stroke: NODE_MARKER_COLOURS[type], strokeOpacity: NODE_MARKER_BORDER_OPACITY })}
  </svg>
}
