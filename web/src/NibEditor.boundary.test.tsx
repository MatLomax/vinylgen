// @vitest-environment jsdom
// Card 18 of vinylgen-on-t3 (tracer `cards/vinylgen-on-t3/build-18-brief.md`): the Nodes stage's
// embedded editor offers nib's boundary tool (`@nib/core` 0.9.2). On a revision's map.json, nib's
// real chrome over the real editor shows:
// - the toolbar button "Draw a boundary", shown on a label map, `aria-pressed` "false" while the
//   tool is off;
// - the B key, which turns the tool on: the button pressed and the status line reading the
//   drafts, "Boundary: 0 drafts"; B again turns it off;
// - the help sheet's B line, "B — draw a boundary; tap a hatched face to label it".
//
// Nothing of `@nib/core` is mocked here. nib's `Editor` needs a 2D context, `Path2D`,
// `ResizeObserver` and animation frames: inert stubs, as in nib's `dom.testkit.ts`, with the
// animation frames queued and run by `frame()`, so the chrome reads the editor's stats.
import { cleanup, render } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { NibEditor } from './NibEditor'

const NODE_FS = 'node:fs'
let MAP = ''
beforeAll(async () => {
  const fs = await import(/* @vite-ignore */ NODE_FS) as { readFileSync(path: string, encoding: 'utf8'): string }
  const here = (import.meta as unknown as { dirname: string }).dirname
  MAP = fs.readFileSync(`${here}/../../tests/fixtures/t3-test-array/map.json`, 'utf8')
})
const MAP_URL = '/api/artifacts/map-1/content'
const BUTTON = ".nib-tbtn[aria-label='Draw a boundary']"
const B_LINE = /^B( — |: )draw a boundary; tap a hatched face to label it$/

const noop = () => {}
const inert = () => new Proxy({} as Record<PropertyKey, unknown>, {
  get: (target, key) => (key in target ? target[key] : key === 'isPointInPath' ? () => false : noop),
  set: (target, key, value) => { target[key] = value; return true },
})
let frames = new Map<number, FrameRequestCallback>()
let nextFrame = 1
// One animation frame: every callback queued before it runs once.
function frame() {
  const due = [...frames.values()]
  frames = new Map()
  for (const callback of due) callback(performance.now())
}

beforeEach(() => {
  frames = new Map()
  vi.stubGlobal('fetch', vi.fn(async (url: string) => String(url) === MAP_URL
    ? new Response(MAP, { status: 200, headers: { 'Content-Type': 'application/json' } })
    : new Response('{}', { status: 404 })))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { const id = nextFrame++; frames.set(id, callback); return id })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => { frames.delete(id) })
  vi.stubGlobal('Path2D', class { constructor() { return inert() } })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => inert() as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) }))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// Whether an element and every ancestor up to the container are shown.
function shown(element: Element | null, container: HTMLElement): boolean {
  if (!element) return false
  for (let e: Element | null = element; e && e !== container; e = e.parentElement) if ((e as HTMLElement).hidden) return false
  return true
}

// The real NibEditor on the fixture's map, nib's chrome mounted and one frame run.
async function mountOnMap() {
  const rendered = render(createElement(NibEditor, { revisionId: 'trace-1', mapUrl: MAP_URL }))
  await vi.waitFor(() => expect(rendered.container.querySelector(BUTTON)).not.toBeNull())
  await vi.waitFor(() => { frame(); expect(rendered.container.querySelector('.nib-status')?.textContent).toMatch(/paths/) })
  return rendered
}
const key = (k: string) => { window.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true })); frame() }

describe('the boundary tool in the Nodes stage\'s editor', () => {
  it('offers the toolbar button "Draw a boundary" on a label map, not pressed', async () => {
    const { container } = await mountOnMap()
    const button = container.querySelector<HTMLButtonElement>(BUTTON)!
    expect(shown(button, container)).toBe(true)
    expect(button.title).toBe('Draw a boundary')
    expect(button.getAttribute('aria-pressed')).toBe('false')
  })

  it('B turns the tool on and off: the button pressed and the status line reading the drafts', async () => {
    const { container } = await mountOnMap()
    const button = container.querySelector<HTMLButtonElement>(BUTTON)!
    key('b')
    expect(button.getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('.nib-status')?.textContent).toBe('Boundary: 0 drafts')
    key('b')
    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect(container.querySelector('.nib-status')?.textContent).toMatch(/paths · \d+ nodes · 0 selected/)
  })

  it('the toolbar button turns the tool on', async () => {
    const { container } = await mountOnMap()
    const button = container.querySelector<HTMLButtonElement>(BUTTON)!
    button.click()
    frame()
    expect(button.getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('.nib-status')?.textContent).toBe('Boundary: 0 drafts')
  })

  it('the help sheet holds the B line', async () => {
    const { container } = await mountOnMap()
    container.querySelector<HTMLButtonElement>("button[aria-label='More']")!.click()
    frame()
    const item = [...container.querySelectorAll<HTMLElement>('.nib-menu-item')].find(b => b.textContent === 'Gesture cheatsheet')!
    item.click()
    frame()
    expect(shown(container.querySelector('.nib-help'), container)).toBe(true)
    const lines = [...container.querySelector('.nib-help-list')!.children].map(li => li.textContent ?? '')
    expect(lines.filter(line => B_LINE.test(line))).toHaveLength(1)
  })
})
