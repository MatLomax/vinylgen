// @vitest-environment jsdom
// Card 8 of vinylgen-on-t3: nib's editor as its own workflow stage, Nodes, after Trace, with nib's
// full chrome (tracer `cards/vinylgen-on-t3/build-8a-brief.md`, vinylgen worklog task
// `nib-s-editor-as-its-own-stage-after-trace-with-nib-s-full-ch`).
//
// - The stage `nodes` ("Nodes", "Edit the traced nodes", lucide's Spline) sits between Trace and
//   Export, and `#stage=nodes` selects it.
// - On it the canvas area is nib's editor (`NibEditor`) on the selected revision's `map.json`,
//   saved for the trace revision that owns the map. A revision without one mounts nothing and
//   the stage's readiness line says "Trace first". While the selected revision's artefacts load,
//   the stage is locked by its lock reason and no editor is mounted.
// - The stage panel holds the heading, the readiness line, the lock reason and a short guidance
//   paragraph; every editing control is nib's chrome, so the panel holds none.
// - The Trace canvas loses the Nodes checkbox and the editor overlay.
// - Closing the panel widens the editor: the canvas's backing store follows its CSS box (nib
//   0.4.0), read through a fake resize observer as nib's own `mount.test.ts` does.
// - Key events on the stage reach the window, where nib's editor binds its keys.
//
// nib's `mountChrome` is a stub here, so the tests pin what vinylgen asks of it (the real
// `mountEditor` of the installed package runs).
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { Spline } from 'lucide-react'
import { createElement, type FunctionComponent } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Project, Revision } from './api'

type NibEditorProps = { revisionId: string; mapUrl?: string | null; onSaved?: (revision: Revision) => void }
type Box = { width: number; height: number }

// The NibEditor App renders: recorded, and either a stand-in or the real component.
const nibStage = vi.hoisted(() => ({ calls: [] as NibEditorProps[], real: false }))
vi.mock('./NibEditor', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('./NibEditor')
  const Real = (actual.NibEditor ?? actual.default) as FunctionComponent<NibEditorProps>
  const Stand = (props: NibEditorProps) => {
    nibStage.calls.push(props)
    return nibStage.real ? createElement(Real, props) : createElement('div', { 'data-testid': 'nib-editor-stub' })
  }
  return { NibEditor: Stand, default: Stand }
})

// nib's chrome, stubbed: records each mount and its notices.
type ChromeCall = { container: HTMLElement; editor: unknown; options: unknown; root: HTMLElement; notices: (string | null)[]; disposed: boolean }
type EditorCall = { canvas: HTMLCanvasElement; options: { text?: string; kind?: string }; mounted: { editor: unknown; loaded: Promise<unknown>; dispose(): void } }
const nibCore = vi.hoisted(() => ({ chromes: [] as ChromeCall[], editors: [] as EditorCall[] }))
vi.mock('@nib/core', async importOriginal => {
  const original = await importOriginal<Record<string, unknown>>()
  const mountEditor = original.mountEditor as (canvas: HTMLCanvasElement, options: EditorCall['options']) => EditorCall['mounted']
  return {
    ...original,
    mountEditor: vi.fn((canvas: HTMLCanvasElement, options: EditorCall['options']) => {
      const mounted = mountEditor(canvas, options)
      nibCore.editors.push({ canvas, options, mounted })
      return mounted
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
      const call: ChromeCall = { container, editor, options, root, notices: [], disposed: false }
      nibCore.chromes.push(call)
      return { root, notice: (text: string | null) => { call.notices.push(text) }, dispose: () => { call.disposed = true; root.remove() } }
    }),
  }
})

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(),
  inkscapeHandoff: vi.fn(), health: vi.fn(), followJob: vi.fn(),
}))
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})

// The test array's map.json, read as the exact text t3 wrote (node:fs through a variable
// specifier: Vite serves nothing outside web/, and the app's tsconfig has no Node types).
const NODE_FS = 'node:fs'
async function fixtureMap(): Promise<string> {
  const fs = await import(/* @vite-ignore */ NODE_FS) as { readFileSync(path: string, encoding: 'utf8'): string }
  const here = (import.meta as unknown as { dirname: string }).dirname
  return fs.readFileSync(`${here}/../../tests/fixtures/t3-test-array/map.json`, 'utf8')
}

const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }
const base: Revision = { id: 'revision-1', project_id: project.id, parent_revision_id: null, kind: 'source', message: null, settings: {}, created_at: '2026-01-01' }
const flatten: Revision = { ...base, id: 'palette-1', parent_revision_id: 'revision-1', kind: 'palette', settings: { palette: {} }, created_at: '2026-01-02' }
const trace: Revision = { ...base, id: 'trace-1', parent_revision_id: 'palette-1', kind: 'trace', settings: { trace: {} }, created_at: '2026-01-03' }
const trace2: Revision = { ...trace, id: 'trace-2', created_at: '2026-01-04' }
function artifact(kind: string, id: string, revisionId: string, mediaType = 'image/png', filename = `${kind}.png`): Artifact {
  return { id, revision_id: revisionId, kind, filename, media_type: mediaType, byte_size: 1, download_url: `/api/artifacts/${id}/content` }
}
const raster = [
  artifact('source', 'source-id', 'revision-1'), artifact('flattened', 'flattened-id', 'palette-1'),
  artifact('segmentation', 'segmentation-id', 'palette-1', 'application/octet-stream', 'segmentation.npz'),
]
const edge = (revisionId = 'trace-1') => artifact('traced-edge', `edge-${revisionId}`, revisionId, 'image/svg+xml', 'traced-edge.svg')
const preview = (revisionId = 'trace-1') => artifact('traced-preview', `preview-${revisionId}`, revisionId, 'image/png', 'traced.png')
const map = (revisionId = 'trace-1') => artifact('traced-map', `map-${revisionId}`, revisionId, 'application/json', 'map.json')

// A browser for nib's editor: inert 2D contexts and Path2D (nib's dom.testkit.ts), frames that
// never run, layout boxes the test sets, and a ResizeObserver whose entries the test delivers.
const noop = () => {}
const inert = () => new Proxy({} as Record<PropertyKey, unknown>, {
  get: (target, key) => (key in target ? target[key] : key === 'isPointInPath' ? () => false : noop),
  set: (target, key, value) => { target[key] = value; return true },
})
const boxes = new Map<Element, Box>()
const observers = new Set<{ targets: Set<Element>; deliver(el: Element): void }>()
function resize(el: Element) { for (const observer of [...observers]) observer.deliver(el) }
let fetchCalls: [string, RequestInit | undefined][] = []
let mapText = ''

function stubBrowser() {
  fetchCalls = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    fetchCalls.push([String(url), init])
    if (String(url).startsWith('/api/artifacts/map-') && (init?.method ?? 'GET') === 'GET') return new Response(mapText, { status: 200, headers: { 'Content-Type': 'application/json' } })
    return new Response(JSON.stringify({ detail: `unexpected ${url}` }), { status: 404, headers: { 'Content-Type': 'application/json' } })
  }))
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.stubGlobal('ResizeObserver', class {
    targets = new Set<Element>()
    constructor(private cb: ResizeObserverCallback) { observers.add(this) }
    observe(el: Element) { this.targets.add(el) }
    unobserve(el: Element) { this.targets.delete(el) }
    disconnect() { this.targets.clear(); observers.delete(this) }
    deliver(el: Element) {
      if (!this.targets.has(el)) return
      const box = boxes.get(el) ?? { width: 0, height: 0 }
      this.cb([{ target: el, contentRect: box } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver)
    }
  })
  vi.stubGlobal('requestAnimationFrame', () => 1)
  vi.stubGlobal('cancelAnimationFrame', noop)
  vi.stubGlobal('Path2D', class { constructor() { return inert() } })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => inert() as unknown as CanvasRenderingContext2D)
  // The stylesheet sizes nib's canvas to its container; jsdom has no layout, so its box is given.
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLCanvasElement) {
    const { width, height } = boxes.get(this) ?? { width: 800, height: 600 }
    return { x: 0, y: 0, left: 0, top: 0, right: width, bottom: height, width, height, toJSON: () => ({}) } as DOMRect
  })
}

function serve(revisions: Revision[], artefacts: (id: string) => Promise<Artifact[]>) {
  mocks.projects.mockResolvedValue([project])
  mocks.project.mockResolvedValue({ ...project, revisions })
  mocks.effectiveArtifacts.mockImplementation((id: string) => artefacts(id))
  mocks.artifacts.mockResolvedValue([])
  mocks.editor.mockResolvedValue({ width: 400, height: 400, protected_pixels: 0, palette: [] })
  mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
  mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
  mocks.followJob.mockReturnValue(() => undefined)
}

async function openApp(stage: string, revision = 'trace-1') {
  window.history.replaceState(null, '', `/#stage=${stage}&project=${project.id}&revision=${revision}`)
  render(createElement((await import('./App')).default))
  await screen.findByRole('tab', { name: 'Trace' })
}

const nodesPanel = () => document.getElementById('workflow-panel-nodes')
const workspace = () => document.querySelector<HTMLElement>('section.workspace')!
const stagePanel = () => document.querySelector<HTMLElement>('aside.stage-panel')!
// The NibEditor App renders with a map, and the stand-in's elements on the page.
const mountedWithMap = () => nibStage.calls.filter(props => props.mapUrl)
const standIns = () => screen.queryAllByTestId('nib-editor-stub')

beforeEach(async () => {
  vi.clearAllMocks()
  nibStage.calls = []
  nibStage.real = false
  nibCore.chromes = []
  nibCore.editors = []
  boxes.clear()
  observers.clear()
  mapText = await fixtureMap()
  localStorage.clear()
  window.history.replaceState(null, '', '/')
  window.scrollTo = vi.fn()
  stubBrowser()
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('the Nodes stage', () => {
  it('is listed between Trace and Export, as "Nodes", "Edit the traced nodes", with lucide\'s Spline icon', async () => {
    serve([trace, flatten, base], async () => [...raster, edge(), map()])
    await openApp('trace')
    expect(screen.getAllByRole('tab').map(tab => tab.getAttribute('aria-label')))
      .toEqual(['Create', 'Upload', 'Flatten', 'Palette', 'Cleanup', 'Edit', 'Trace', 'Nodes', 'Export'])
    const tab = screen.getByRole('tab', { name: 'Nodes' })
    expect(tab.querySelector('strong')?.textContent).toBe('Nodes')
    expect(tab.querySelector('small')?.textContent).toBe('Edit the traced nodes')
    // The icon is lucide's Spline, drawn exactly as lucide-react draws it.
    const icon = tab.querySelector('svg')
    expect(icon).toBeTruthy()
    const spline = render(createElement(Spline)).container.querySelector('svg')!
    expect(icon!.innerHTML).toBe(spline.innerHTML)
    expect(tab.getAttribute('aria-controls')).toBe('workflow-panel-nodes')
  })

  it('is selected by #stage=nodes, and choosing it writes the hash', async () => {
    const { stageFromHash } = await import('./App')
    expect(stageFromHash('#stage=nodes')).toBe('nodes')
    expect(stageFromHash('#stage=nodes&project=project-1&revision=trace-1')).toBe('nodes')

    serve([trace, flatten, base], async () => [...raster, edge(), map()])
    await openApp('nodes')
    expect(screen.getByRole('tab', { name: 'Nodes' }).getAttribute('aria-selected')).toBe('true')
    expect(nodesPanel()).toBeTruthy()

    fireEvent.click(screen.getByRole('tab', { name: 'Trace' }))
    await vi.waitFor(() => expect(new URLSearchParams(window.location.hash.slice(1)).get('stage')).toBe('trace'))
    fireEvent.click(screen.getByRole('tab', { name: 'Nodes' }))
    await vi.waitFor(() => expect(new URLSearchParams(window.location.hash.slice(1)).get('stage')).toBe('nodes'))
    expect(screen.getByRole('tab', { name: 'Nodes' }).getAttribute('aria-selected')).toBe('true')
    expect(new URLSearchParams(window.location.hash.slice(1)).get('revision')).toBe('trace-1')
  })

  it('shows nib\'s editor on the revision\'s map.json in the canvas area, for the revision that owns the map, and nothing else there', async () => {
    // The selected revision's effective artefacts: the map is its parent trace's own.
    const owner = map('trace-1')
    serve([trace2, trace, flatten, base], async () => [...raster, edge('trace-1'), preview('trace-1'), owner])
    await openApp('nodes', 'trace-2')
    await vi.waitFor(() => expect(standIns()).toHaveLength(1))
    expect(mountedWithMap().at(-1)).toMatchObject({ revisionId: 'trace-1', mapUrl: owner.download_url })
    expect(typeof mountedWithMap().at(-1)?.onSaved).toBe('function')
    // The editor fills the canvas area: in the workspace, not in the stage panel, with no
    // image, Trace canvas or inspection controls beside it.
    const editor = standIns()[0]
    expect(workspace().contains(editor)).toBe(true)
    expect(stagePanel().contains(editor)).toBe(false)
    const canvasArea = [...workspace().querySelectorAll('img')].filter(image => !stagePanel().contains(image))
    expect(canvasArea).toHaveLength(0)
    expect(document.querySelector('[role="group"][aria-label="Inspection layers"]')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Zoom in' })).toBeNull()
    // The readiness line does not ask for a trace.
    expect(nodesPanel()!.textContent).not.toContain('Trace first')
  })

  it('holds the heading, the readiness line and a guidance paragraph in its panel, and no editing control', async () => {
    serve([trace, flatten, base], async () => [...raster, edge(), map()])
    await openApp('nodes')
    await vi.waitFor(() => expect(standIns()).toHaveLength(1))
    const panel = nodesPanel()!
    expect(stagePanel().contains(panel)).toBe(true)
    expect(panel.querySelector('h2')?.textContent?.trim()).toBeTruthy()
    expect(panel.querySelector('.section-heading')).toBeTruthy()
    // The guidance: what is edited, and that a save makes a new revision.
    const guidance = [...panel.querySelectorAll('p')].map(paragraph => paragraph.textContent ?? '').join(' ')
    expect(guidance).toMatch(/node/i)
    expect(guidance).toMatch(/\bsave\b/i)
    expect(guidance).toMatch(/new (trace )?revision/i)
    // Every editing control is nib's chrome, none is the panel's.
    expect(panel.querySelectorAll('button, input, select, textarea')).toHaveLength(0)
  })

  it('mounts nothing for a revision with no map.json, and its readiness line says "Trace first"', async () => {
    // Traced before t3: an edge SVG and no map.json.
    serve([trace, flatten, base], async () => [...raster, edge()])
    await openApp('nodes')
    await vi.waitFor(() => expect(mocks.effectiveArtifacts).toHaveBeenCalledWith('trace-1', expect.anything()))
    await vi.waitFor(() => expect(nodesPanel()?.querySelector('.section-heading')?.textContent).toContain('Trace first'))
    expect(mountedWithMap()).toHaveLength(0)
    expect(workspace().querySelector('canvas')).toBeNull()
    cleanup()

    // Not traced at all: a palette revision.
    nibStage.calls = []
    serve([flatten, base], async () => raster)
    await openApp('nodes', 'palette-1')
    await vi.waitFor(() => expect(mocks.effectiveArtifacts).toHaveBeenCalledWith('palette-1', expect.anything()))
    await vi.waitFor(() => expect(nodesPanel()?.querySelector('.section-heading')?.textContent).toContain('Trace first'))
    expect(mountedWithMap()).toHaveLength(0)
  })

  it('is locked by the lock reason while the selected revision\'s artefacts load, with no editor on the last revision\'s map', async () => {
    let release: (rows: Artifact[]) => void = noop
    const pending = new Promise<Artifact[]>(resolve => { release = resolve })
    serve([trace2, trace, flatten, base], async id => id === 'trace-2' ? pending : [...raster, edge('trace-1'), map('trace-1')])
    await openApp('nodes', 'trace-1')
    await vi.waitFor(() => expect(standIns()).toHaveLength(1))
    expect(mountedWithMap().at(-1)?.mapUrl).toBe(map('trace-1').download_url)

    // Choose trace-2; its artefacts are still on their way.
    fireEvent.change(screen.getByLabelText('Revision'), { target: { value: 'trace-2' } })
    await vi.waitFor(() => expect(nodesPanel()!.textContent).toContain('Loading the selected revision…'))
    expect(standIns()).toHaveLength(0)

    // They arrive: the editor opens trace-2's own map.
    await act(async () => { release([...raster, edge('trace-2'), map('trace-2')]) })
    await vi.waitFor(() => expect(mountedWithMap().at(-1)?.mapUrl).toBe(map('trace-2').download_url))
    expect(standIns()).toHaveLength(1)
    expect(mountedWithMap().at(-1)?.revisionId).toBe('trace-2')
    expect(nodesPanel()!.textContent).not.toContain('Loading the selected revision…')
  })
})

describe('the Trace canvas', () => {
  it('has no Nodes checkbox and no editor, and shows the Flattened image when it is chosen', async () => {
    serve([trace, flatten, base], async () => [...raster, edge(), preview(), map()])
    await openApp('trace')
    await vi.waitFor(() => expect(document.querySelector('.artwork-surface img')).toBeTruthy())
    for (const image of document.querySelectorAll<HTMLImageElement>('.artwork-surface img')) {
      Object.defineProperties(image, { naturalWidth: { configurable: true, value: 400 }, naturalHeight: { configurable: true, value: 400 } })
      fireEvent.load(image)
    }
    expect(screen.queryByRole('checkbox', { name: 'Nodes' })).toBeNull()
    const group = document.querySelector<HTMLElement>('.artwork-surface [role="group"][aria-label="Inspection layers"]')
    if (group) expect(within(group).queryByText('Nodes')).toBeNull()
    expect(nibStage.calls).toHaveLength(0)

    // Choosing Flattened shows the flattened image, and nothing covers it.
    const views = document.querySelector<HTMLElement>('header [role="group"][aria-label="Artwork view"]')!
    fireEvent.click(within(views).getByRole('button', { name: 'Flattened' }))
    await vi.waitFor(() => expect(document.querySelector<HTMLImageElement>('.artwork-surface .image-stage > img')?.getAttribute('src')).toBe('/api/artifacts/flattened-id/content'))
    expect(nibStage.calls).toHaveLength(0)
    expect(document.querySelector('.artwork-surface canvas.nib-editor-canvas, .artwork-surface .nib-editor')).toBeNull()
  })
})

describe('nib\'s editor on the Nodes stage', () => {
  async function openEditor() {
    nibStage.real = true
    serve([trace, flatten, base], async () => [...raster, edge(), map()])
    await openApp('nodes')
    await vi.waitFor(() => expect(nibCore.editors).toHaveLength(1))
    await nibCore.editors[0].mounted.loaded
    await vi.waitFor(() => expect(nibCore.chromes).toHaveLength(1))
    return { canvas: nibCore.editors[0].canvas, chrome: nibCore.chromes[0] }
  }

  it('is nib\'s editor with nib\'s chrome offering Save only, over the canvas', async () => {
    const { canvas, chrome } = await openEditor()
    expect(nibCore.editors[0].options).toMatchObject({ text: mapText, kind: 'label-map' })
    expect(chrome.editor).toBe(nibCore.editors[0].mounted.editor)
    expect(chrome.options).toEqual({ actions: ['save'] })
    expect(chrome.container.contains(canvas)).toBe(true)
    expect(workspace().contains(canvas)).toBe(true)
    expect(stagePanel().contains(canvas)).toBe(false)
  })

  it('widens with the closed panel: the canvas\'s backing store follows its box, the CSS size stays the stylesheet\'s, and the editor is not remounted', async () => {
    const { canvas } = await openEditor()
    expect(canvas.classList.contains('nib-editor-canvas')).toBe(true)
    expect([canvas.width, canvas.height]).toEqual([800, 600])
    expect(canvas.style.width).toBe('')
    expect(canvas.style.height).toBe('')

    // Close the stage panel: the canvas area widens, the layout gives the canvas its new box,
    // and the observer reports it.
    fireEvent.click(screen.getByRole('button', { name: 'Hide stage settings' }))
    await vi.waitFor(() => expect(workspace().classList.contains('panel-closed')).toBe(true))
    boxes.set(canvas, { width: 1180, height: 600 })
    act(() => resize(canvas))
    expect([canvas.width, canvas.height]).toEqual([1180, 600])
    expect(canvas.style.width).toBe('')
    expect(canvas.style.height).toBe('')
    expect(canvas.isConnected).toBe(true)
    expect(nibCore.editors).toHaveLength(1)
    expect(nibCore.chromes).toHaveLength(1)
    expect(nibCore.chromes[0].disposed).toBe(false)
  })

  it('lets key events on the stage reach the window, where nib binds its keys', async () => {
    const { canvas, chrome } = await openEditor()
    const reached: EventTarget[] = []
    const listen = (event: KeyboardEvent) => { if (event.key === 'F13') reached.push(event.target!) }
    window.addEventListener('keydown', listen)
    try {
      for (const target of [canvas, chrome.root, chrome.container, nodesPanel()!]) {
        target.dispatchEvent(new KeyboardEvent('keydown', { key: 'F13', bubbles: true, cancelable: true }))
      }
    } finally { window.removeEventListener('keydown', listen) }
    expect(reached).toEqual([canvas, chrome.root, chrome.container, nodesPanel()])
  })
})
