// @vitest-environment jsdom
// Card 8 of vinylgen-on-t3 (tracer `cards/vinylgen-on-t3/build-8a-brief.md`): `NibEditor` is nib's
// editor with nib's full chrome. It owns a canvas and mounts nib's `Editor` on it with
// `mountEditor` (`@nib/core` 0.8.0, from `web/vendor/nib-core-0.8.0.tgz`) through a server host:
// `open` is a GET of the revision's `map.json` artefact, `save` a POST of the exact text nib wrote
// to `/api/revisions/{revision_id}/map` (`application/json`). Over the canvas it mounts nib's
// chrome with `mountChrome(container, editor, { actions: ['save'] })` and imports its stylesheet
// `@nib/core/chrome.css`. It keeps no bar of its own: Save is the chrome's, and the outcome of a
// save is the chrome's `notice(text)`, never an element of the component's. With no `mapUrl` it
// renders nothing.
//
// `mountChrome` is a stub that records its mount and
// its notices and offers a Save button calling `editor.saveDocument()`, as nib's chrome does. The
// installed package's real `mountEditor` runs. nib's `Editor` needs a 2D context, `Path2D`,
// `ResizeObserver` and animation frames: inert stubs, as in nib's `dom.testkit.ts`.
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { createElement, type FunctionComponent } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

const NODE_FS = 'node:fs'
let MAP = ''
beforeAll(async () => {
  const fs = await import(/* @vite-ignore */ NODE_FS) as { readFileSync(path: string, encoding: 'utf8'): string }
  const here = (import.meta as unknown as { dirname: string }).dirname
  MAP = fs.readFileSync(`${here}/../../tests/fixtures/t3-test-array/map.json`, 'utf8')
})
const MAP_URL = '/api/artifacts/map-1/content'
const REVISION = 'trace-1'
const UNCHANGED = { detail: 'Nothing was edited: the map is unchanged.', reason: 'unchanged', problems: [] as string[] }

type Call = [string, RequestInit | undefined]
type NibEditorProps = { revisionId: string; mapUrl?: string | null; onSaved?: (revision: unknown) => void }
type ChromeCall = { container: HTMLElement; editor: unknown; options: unknown; root: HTMLElement; save: HTMLButtonElement; notices: (string | null)[]; disposed: boolean }
type Mounted = { editor: unknown; loaded: Promise<unknown>; dispose(): void }
type EditorCall = { canvas: HTMLCanvasElement; options: { text?: string; kind?: string; host?: unknown }; mounted: Mounted; disposed: boolean }

const nibCore = vi.hoisted(() => ({ chromes: [] as ChromeCall[], editors: [] as EditorCall[] }))
vi.mock('@nib/core', async importOriginal => {
  const original = await importOriginal<Record<string, unknown>>()
  const mountEditor = original.mountEditor as (canvas: HTMLCanvasElement, options: EditorCall['options']) => Mounted
  return {
    ...original,
    mountEditor: vi.fn((canvas: HTMLCanvasElement, options: EditorCall['options']) => {
      const mounted = mountEditor(canvas, options)
      const call: EditorCall = { canvas, options, mounted, disposed: false }
      nibCore.editors.push(call)
      const dispose = mounted.dispose.bind(mounted)
      return { ...mounted, editor: mounted.editor, loaded: mounted.loaded, dispose: () => { call.disposed = true; dispose() } }
    }),
    mountChrome: vi.fn((container: HTMLElement, editor: { saveDocument(): Promise<unknown> }, options?: unknown) => {
      const root = container.ownerDocument.createElement('div')
      root.className = 'nib-chrome'
      const save = container.ownerDocument.createElement('button')
      save.type = 'button'
      save.setAttribute('aria-label', 'Save label map')
      save.addEventListener('click', () => { void editor.saveDocument() })
      root.append(save)
      container.append(root)
      const call: ChromeCall = { container, editor, options, root, save, notices: [], disposed: false }
      nibCore.chromes.push(call)
      return { root, notice: (text: string | null) => { call.notices.push(text) }, dispose: () => { call.disposed = true; root.remove() } }
    }),
  }
})

async function realNibEditor(): Promise<FunctionComponent<NibEditorProps>> {
  const module = await vi.importActual<Record<string, unknown>>('./NibEditor')
  return (module.NibEditor ?? module.default) as FunctionComponent<NibEditorProps>
}

const noop = () => {}
const inert = () => new Proxy({} as Record<PropertyKey, unknown>, {
  get: (target, key) => (key in target ? target[key] : key === 'isPointInPath' ? () => false : noop),
  set: (target, key, value) => { target[key] = value; return true },
})
let fetchCalls: Call[] = []

function stubBrowser(respond: (url: string, init?: RequestInit) => Promise<Response>) {
  fetchCalls = []
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => { fetchCalls.push([String(url), init]); return respond(String(url), init) }))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('requestAnimationFrame', () => 1)
  vi.stubGlobal('cancelAnimationFrame', noop)
  vi.stubGlobal('Path2D', class { constructor() { return inert() } })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => inert() as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) }))
}

const json = (body: unknown, status: number) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const method = (init?: RequestInit) => (init?.method ?? 'GET').toUpperCase()
async function bodyText(init?: RequestInit): Promise<string> {
  const body = init?.body
  return typeof body === 'string' ? body : new Response(body ?? null).text()
}
function serveMap(answer: () => Response) {
  stubBrowser(async (url, init) => {
    if (url === MAP_URL && method(init) === 'GET') return new Response(MAP, { status: 200, headers: { 'Content-Type': 'application/json' } })
    if (url === `/api/revisions/${REVISION}/map` && method(init) === 'POST') return answer()
    return json({ detail: `unexpected ${method(init)} ${url}` }, 404)
  })
}

// Render the real component and wait for nib's editor and chrome over it, its map loaded.
async function mountOnMap(props: Partial<NibEditorProps> = {}) {
  const NibEditor = await realNibEditor()
  const rendered = render(createElement(NibEditor, { revisionId: REVISION, mapUrl: MAP_URL, ...props }))
  await vi.waitFor(() => expect(nibCore.editors).toHaveLength(1))
  await nibCore.editors[0].mounted.loaded
  await vi.waitFor(() => expect(nibCore.chromes).toHaveLength(1))
  return { ...rendered, editor: nibCore.editors[0], chrome: nibCore.chromes[0] }
}
// Elements the component renders itself, outside nib's chrome.
const ownElements = (container: HTMLElement, chrome: ChromeCall, selector: string) =>
  [...container.querySelectorAll(selector)].filter(element => !chrome.root.contains(element))

beforeEach(() => {
  vi.clearAllMocks()
  nibCore.chromes = []
  nibCore.editors = []
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('NibEditor', () => {
  it('mounts nib\'s editor on the revision\'s map.json and nib\'s chrome over it, offering Save only', async () => {
    serveMap(() => json(UNCHANGED, 422))
    const { container, editor, chrome } = await mountOnMap()
    // The host's open: a GET of the artefact; the editor opens its exact text as a label map.
    expect(fetchCalls.some(([url, init]) => url === MAP_URL && method(init) === 'GET')).toBe(true)
    expect(container.contains(editor.canvas)).toBe(true)
    expect(editor.options).toMatchObject({ text: MAP, kind: 'label-map' })
    // The chrome: on the editor mountEditor returned, in a container holding the canvas, with
    // the one action vinylgen's server takes.
    expect(chrome.editor).toBe(editor.mounted.editor)
    expect(chrome.options).toEqual({ actions: ['save'] })
    expect(container.contains(chrome.container)).toBe(true)
    expect(chrome.container.contains(editor.canvas)).toBe(true)
    // No bar of its own: no button, no status line, no alert outside the chrome.
    expect(ownElements(container, chrome, 'button')).toHaveLength(0)
    expect(ownElements(container, chrome, '[role="status"], [role="alert"]')).toHaveLength(0)
    expect(container.querySelector('.nib-editor-bar')).toBeNull()
  })

  it('saves through the chrome\'s Save: the map nib wrote, byte for byte; the server\'s "unchanged" is the chrome\'s notice', async () => {
    serveMap(() => json(UNCHANGED, 422))
    const onSaved = vi.fn()
    const { container, chrome } = await mountOnMap({ onSaved })
    await act(async () => { fireEvent.click(chrome.save) })
    await vi.waitFor(() => expect(fetchCalls.some(([, init]) => method(init) === 'POST')).toBe(true))
    const posts = fetchCalls.filter(([, init]) => method(init) === 'POST')
    expect(posts).toHaveLength(1)
    const [postUrl, postInit] = posts[0]
    expect(postUrl).toBe(`/api/revisions/${REVISION}/map`)
    expect(new Headers(postInit?.headers).get('Content-Type')).toMatch(/^application\/json\b/)
    expect(await bodyText(postInit)).toBe(MAP)
    await vi.waitFor(() => expect(chrome.notices.at(-1)).toBe(UNCHANGED.detail))
    expect(onSaved).not.toHaveBeenCalled()
    expect(ownElements(container, chrome, '[role="status"], [role="alert"]')).toHaveLength(0)
  })

  it('renders no editor for a revision with no map.json', async () => {
    stubBrowser(async url => json({ detail: `unexpected ${url}` }, 404))
    const NibEditor = await realNibEditor()
    const { container } = render(createElement(NibEditor, { revisionId: 'palette-1', mapUrl: null }))
    await act(async () => { await Promise.resolve() })
    expect(container.querySelector('canvas')).toBeNull()
    expect(container.querySelector('button')).toBeNull()
    expect(nibCore.editors).toHaveLength(0)
    expect(nibCore.chromes).toHaveLength(0)
    expect(fetchCalls).toHaveLength(0)
  })

  it('disposes nib\'s chrome and editor when it unmounts', async () => {
    serveMap(() => json(UNCHANGED, 422))
    const { unmount, editor, chrome } = await mountOnMap()
    expect(chrome.root.isConnected).toBe(true)
    unmount()
    expect(chrome.disposed).toBe(true)
    expect(editor.disposed).toBe(true)
    expect(chrome.root.isConnected).toBe(false)
  })

  // Last in the file: it resets the module registry.
  it('imports nib\'s chrome stylesheet, @nib/core/chrome.css', async () => {
    const loaded = { css: false }
    vi.resetModules()
    vi.doMock('@nib/core/chrome.css', () => { loaded.css = true; return {} })
    try {
      await vi.importActual('./NibEditor')
    } finally {
      vi.doUnmock('@nib/core/chrome.css')
    }
    expect(loaded.css).toBe(true)
  })
})
