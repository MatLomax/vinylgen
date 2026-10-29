// @vitest-environment jsdom
// Card 4 of vinylgen-on-t3 (tracer card `cards/vinylgen-on-t3/card.md` section 2, the
// `PathNodes.tsx` row): until the nib embed lands, the Nodes overlay draws the nodes of a path
// with no `A` (an old revision's `M L C Z` paths) as today, and for a path holding an `A` draws
// no node positions and shows the path's node count from `sodipodi:nodetypes` (its letters
// less one per closed subpath, the Inkscape count). An `A` is one node or more in Inkscape, so
// taking each arc's end point as one node puts the letters and the positions out of step.
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'
import { parseTracedNodes } from './PathNodes'

const SODIPODI = 'http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd'
const svgDocument = (body: string, viewBox = '0 0 1000 700') =>
  `<?xml version='1.0' encoding='utf-8'?>\n<svg xmlns="http://www.w3.org/2000/svg" xmlns:sodipodi="${SODIPODI}" viewBox="${viewBox}">${body}</svg>`

// An old revision's paths: absolute M L C Z only (PathNodes.test.tsx's committed trace).
const OLD_LAYERS = `
  <g id="layer-red"><path id="a" d="M100 100 L300 100 C400 100 400 300 300 300 L100 100 Z" sodipodi:nodetypes="cscz"/></g>
  <g id="layer-blue"><path id="b" d="M300 100 L500 100 L300 300 Z" sodipodi:nodetypes="zccz"/></g>`
const OLD = svgDocument(OLD_LAYERS)
// Face 3 of the t3 test array (tests/fixtures/t3-test-array/paths.json), as t3 writes it: one
// closed subpath of arcs and lines, 12 nodetypes letters, so 11 nodes in Inkscape.
const FACE_3_D = 'M122.0 0.0A62.96262339471195 62.96262339471195 0.0 0 1 73.5 58.0A22.33126106157658 31.007124238727407 27.407303715825794 0 0 91.47518585833012 14.542485705706028A22.33126106157658 31.007124238727407 27.407303715825794 0 0 56.5 11.0A10.00204296102602 10.00204296102602 0.0 0 0 59.69073594815177 27.393926983060002A10.00204296102602 10.00204296102602 0.0 0 0 73.5 18.0A27.043634122880558 43.126623323529486 -88.71060069060786 0 1 0.0 45.0L0.0 60.0A85.10659119750731 85.10659119750731 0.0 0 0 134.0 0.0L122.0 0.0Z'
const FACE_3_NODETYPES = 'ccssscsccscc'
const FACE_3_NODES = FACE_3_NODETYPES.length - 1
const MIXED = svgDocument(`${OLD_LAYERS}
  <g id="layer-green"><path id="face-3" d="${FACE_3_D}" sodipodi:nodetypes="${FACE_3_NODETYPES}"/></g>`)

describe('PathNodes on t3 paths', () => {
  it('draws an old revision\'s M L C Z nodes as today and no position of a path holding an A', () => {
    const old = parseTracedNodes(OLD)
    const mixed = parseTracedNodes(MIXED)
    expect([...mixed.xs]).toEqual([...old.xs])
    expect([...mixed.ys]).toEqual([...old.ys])
    expect([...mixed.types]).toEqual([...old.types])
    expect(mixed.counts).toEqual({ cusp: 2, symmetric: 1, smooth: 1 })
    // The A path's nodes are typed, so they are not "without a recorded type".
    expect(mixed.untyped).toBe(0)
  })
})

describe('Trace canvas Nodes overlay on a path holding an A', () => {
  const layers: WorkspaceLayer[] = [
    { id: 'svg', name: 'Traced SVG', url: '/traced.svg', mediaType: 'image/svg+xml', role: 'image' },
    { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
  ]
  type Pending = { url: string; resolve: (response: unknown) => void }
  let pending: Pending[] = []
  let drawn: { name: string; args: number[] }[] = []

  beforeEach(() => {
    pending = []
    drawn = []
    localStorage.clear()
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
    vi.stubGlobal('fetch', vi.fn((url: string) => new Promise(resolve => pending.push({ url, resolve }))))
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
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    delete (HTMLCanvasElement.prototype as { clientWidth?: number }).clientWidth
    delete (HTMLCanvasElement.prototype as { clientHeight?: number }).clientHeight
    localStorage.clear()
  })

  function prepare() {
    const result = render(<ImageWorkspace layers={layers} preferredLayerId="svg" editorMode canvasLabel="Trace canvas" pathNodes={{ url: '/traced.svg' }} />)
    const viewport = within(result.container).getByLabelText('Image comparison viewport')
    Object.defineProperties(viewport, { clientWidth: { configurable: true, value: 800 }, clientHeight: { configurable: true, value: 600 } })
    viewport.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) })
    for (const image of result.container.querySelectorAll('img')) {
      Object.defineProperties(image, { naturalWidth: { configurable: true, value: 1000 }, naturalHeight: { configurable: true, value: 700 } })
      fireEvent.load(image)
    }
    return result
  }
  // The filled marker pass, as PathNodes.test.tsx counts it: one rect, arc or closePath per marker.
  function markers() {
    const passes: { name: string }[][] = []
    for (const call of drawn) {
      if (call.name === 'beginPath') passes.push([])
      else passes[passes.length - 1]?.push(call)
    }
    return passes.filter(pass => pass.some(call => call.name === 'fill')).flat()
      .filter(call => call.name === 'rect' || call.name === 'arc' || call.name === 'closePath').length
  }

  it('marks only the M L C Z nodes and shows the A path\'s node count from its nodetypes', async () => {
    const { container } = prepare()
    fireEvent.click(within(screen.getByRole('group', { name: 'Inspection layers' })).getByLabelText('Nodes'))
    const request = pending.find(entry => entry.url === '/traced.svg')!
    await act(async () => { request.resolve({ ok: true, status: 200, text: async () => MIXED }) })
    expect(markers()).toBe(4)
    const legend = screen.getByRole('list', { name: 'Node types' })
    expect([...legend.querySelectorAll('.node-count')].map(count => Number(count.textContent))).toEqual([2, 1, 1])
    // The A path's count is shown beside the legend, not as positions or as a typed count.
    const note = [...container.querySelector('.node-legend')!.children].filter(child => child !== legend).map(child => child.textContent ?? '').join(' ')
    expect(note).toMatch(new RegExp(`(?<![\\d.,])${FACE_3_NODES}(?![\\d.,])`))
  })
})
