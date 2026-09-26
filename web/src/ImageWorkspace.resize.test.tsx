// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'
import { fitTransform } from './workspace'

const layers: WorkspaceLayer[] = [
  { id: 'source', name: 'Source', url: '/source.png', mediaType: 'image/png', role: 'image' },
]

// The ResizeObserver callbacks by observed element, fired by `resize` as the browser would.
let observed: Map<Element, ResizeObserverCallback>

beforeEach(() => {
  observed = new Map()
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.stubGlobal('ResizeObserver', class {
    constructor(private readonly callback: ResizeObserverCallback) {}
    observe(target: Element) { observed.set(target, this.callback) }
    unobserve(target: Element) { observed.delete(target) }
    disconnect() { for (const [target, callback] of observed) if (callback === this.callback) observed.delete(target) }
  })
  HTMLElement.prototype.setPointerCapture = () => undefined
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function resize(element: HTMLElement, width: number, height: number) {
  Object.defineProperties(element, { clientWidth: { configurable: true, value: width }, clientHeight: { configurable: true, value: height } })
  element.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: width, bottom: height, width, height, toJSON: () => ({}) })
  act(() => observed.get(element)?.([{ target: element, contentRect: { width, height } } as unknown as ResizeObserverEntry], {} as ResizeObserver))
}

function view(container: HTMLElement) {
  const match = /translate3d\(([-\d.e]+)px, ([-\d.e]+)px, 0\) scale\(([\d.e]+)\)/.exec((container.querySelector('.image-stage') as HTMLElement).style.transform)!
  return { x: Number(match[1]), y: Number(match[2]), scale: Number(match[3]) }
}

// A 1000×700 image on the canvas in an 800×600 viewport, as first laid out.
function prepare() {
  const result = render(<ImageWorkspace layers={layers} editorMode canvasLabel="Flatten canvas" />)
  const viewport = within(result.container).getByLabelText('Image comparison viewport')
  resize(viewport, 800, 600)
  const image = screen.getByAltText('Source') as HTMLImageElement
  Object.defineProperties(image, { naturalWidth: { configurable: true, value: 1000 }, naturalHeight: { configurable: true, value: 700 } })
  fireEvent.load(image)
  resize(viewport, 800, 600)
  return { ...result, viewport }
}

describe('the canvas view through viewport resizes', () => {
  it('refits a view the user has not moved, so a panel collapsing enlarges the image', () => {
    const { container, viewport } = prepare()
    expect(view(container)).toEqual(fitTransform(800, 600, 1000, 700))
    resize(viewport, 1200, 600)
    expect(view(container)).toEqual(fitTransform(1200, 600, 1000, 700))
  })

  it('keeps a zoomed view\'s scale and centre when only the height changes, as a phone\'s address bar does while scrolling', () => {
    const { container, viewport } = prepare()
    fireEvent.click(screen.getByLabelText('Zoom in'))
    fireEvent.click(screen.getByLabelText('Zoom in'))
    const zoomed = view(container)
    resize(viewport, 800, 540)
    expect(view(container)).toEqual({ scale: zoomed.scale, x: zoomed.x, y: zoomed.y - 30 })
    resize(viewport, 800, 600)
    expect(view(container)).toEqual(zoomed)
  })

  it('keeps a panned view when a panel opens or closes beside the canvas', () => {
    const { container, viewport } = prepare()
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 160, clientY: 80 })
    fireEvent.pointerUp(viewport, { pointerId: 1, clientX: 160, clientY: 80 })
    const panned = view(container)
    expect(panned).not.toEqual(fitTransform(800, 600, 1000, 700))
    resize(viewport, 1200, 600)
    expect(view(container)).toEqual({ ...panned, x: panned.x + 200 })
  })

  it('leaves the view fitted after a tap whose press jitters by less than a tap\'s distance', () => {
    const { container, viewport } = prepare()
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 103, clientY: 102 })
    fireEvent.pointerUp(viewport, { pointerId: 1, clientX: 103, clientY: 102 })
    resize(viewport, 800, 400)
    expect(view(container)).toEqual(fitTransform(800, 400, 1000, 700))
  })

  it('refits again on resize once Fit image restores the fitted view', () => {
    const { container, viewport } = prepare()
    fireEvent.click(screen.getByLabelText('Zoom in'))
    fireEvent.click(screen.getByLabelText('Fit image'))
    resize(viewport, 800, 400)
    expect(view(container)).toEqual(fitTransform(800, 400, 1000, 700))
  })
})
