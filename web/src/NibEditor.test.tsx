// @vitest-environment jsdom
// Card 7 of vinylgen-on-t3, the nib embed (tracer card `cards/vinylgen-on-t3/card.md` section 5,
// "The traced-node viewer is nib's editor", and section 8, "Later: the nib embed"). `NibEditor`
// owns a canvas and mounts nib's `Editor` with `mountEditor` (`@nib/core` 0.1.0, from
// `web/vendor/nib-core-0.1.0.tgz`) through a server host: `open` is a GET of the revision's
// `map.json` artefact, `save` a POST of the exact text nib wrote to
// `/api/revisions/{revision_id}/map` (`application/json`). Saving with nothing edited sends the
// fixture's bytes back; the server answers 422 with `reason: "unchanged"`, and the component shows
// that as a notice, not an error. On the Trace stage the Nodes toggle shows `NibEditor` for a
// revision with a `map.json` and nothing for one without; `PathNodes` is gone.
//
// nib's `Editor` needs a 2D context, `Path2D`, `ResizeObserver` and animation frames: the same
// inert stubs as nib's `packages/core/pack.slowtest.ts` supply under Node.
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createElement, type FunctionComponent } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Project, Revision } from './api'

const NIB_CORE = '@nib/core'
// The test array's map.json (tests/fixtures/t3-test-array), read as the exact text t3 wrote. Vite
// serves nothing outside web/, and the app's tsconfig has no Node types, so it is read through a
// node:fs reached by a variable specifier.
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
type NibEditorProps = { revisionId: string; mapUrl?: string | null }

// A stand-in for NibEditor in the App test: records its props.
const nibProps = vi.hoisted(() => ({ calls: [] as NibEditorProps[] }))
vi.mock('./NibEditor', () => {
  const Stub = (props: NibEditorProps) => { nibProps.calls.push(props); return createElement('div', { 'data-testid': 'nib-editor-stub' }) }
  return { NibEditor: Stub, default: Stub }
})

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(),
  inkscapeHandoff: vi.fn(), health: vi.fn(), followJob: vi.fn(),
}))
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})

// The component under test is the real one: `vi.importActual` bypasses the stand-in above.
async function realNibEditor(): Promise<FunctionComponent<NibEditorProps>> {
  const module = await vi.importActual<Record<string, unknown>>('./NibEditor')
  return (module.NibEditor ?? module.default) as FunctionComponent<NibEditorProps>
}

// pack.slowtest.ts's minimal browser: every 2D context and Path2D call is a no-op.
const noop = () => {}
const inert = () => new Proxy({} as Record<PropertyKey, unknown>, {
  get: (target, key) => (key in target ? target[key] : noop),
  set: (target, key, value) => { target[key] = value; return true },
})
let fetchCalls: Call[] = []

function stubBrowser(respond: (url: string, init?: RequestInit) => Promise<Response>) {
  fetchCalls = []
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => { fetchCalls.push([String(url), init]); return respond(String(url), init) }))
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('requestAnimationFrame', () => 1)
  vi.stubGlobal('cancelAnimationFrame', noop)
  vi.stubGlobal('Path2D', class { constructor() { return inert() } })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => inert() as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) }))
  Object.defineProperty(HTMLCanvasElement.prototype, 'clientWidth', { configurable: true, get: () => 800 })
  Object.defineProperty(HTMLCanvasElement.prototype, 'clientHeight', { configurable: true, get: () => 600 })
}

const json = (body: unknown, status: number) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const method = (init?: RequestInit) => (init?.method ?? 'GET').toUpperCase()
async function bodyText(init?: RequestInit): Promise<string> {
  const body = init?.body
  return typeof body === 'string' ? body : new Response(body ?? null).text()
}

beforeEach(() => {
  vi.clearAllMocks()
  nibProps.calls = []
  localStorage.clear()
  window.history.replaceState(null, '', '/')
  window.scrollTo = vi.fn()
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  delete (HTMLCanvasElement.prototype as { clientWidth?: number }).clientWidth
  delete (HTMLCanvasElement.prototype as { clientHeight?: number }).clientHeight
  localStorage.clear()
})

describe('NibEditor', () => {
  it('mounts nib\'s editor on the revision\'s map.json and saves it back byte for byte; the 422 "unchanged" is a notice', async () => {
    // The installed package reads and writes the test array's map.json byte for byte. The
    // specifier is a variable so that, until the package is installed, this test alone fails on
    // its resolution instead of the whole file failing to transform.
    const nib = await import(/* @vite-ignore */ NIB_CORE) as typeof import('@nib/core')
    expect(nib.writeLabelMap(nib.readLabelMap(MAP))).toBe(MAP)

    stubBrowser(async (url, init) => {
      if (url === MAP_URL && method(init) === 'GET') return new Response(MAP, { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (url === `/api/revisions/${REVISION}/map` && method(init) === 'POST') return json(UNCHANGED, 422)
      return json({ detail: `unexpected ${method(init)} ${url}` }, 404)
    })
    const NibEditor = await realNibEditor()
    const { container } = render(createElement(NibEditor, { revisionId: REVISION, mapUrl: MAP_URL }))

    // The host's open: a GET of the artefact. The component owns the canvas the editor draws on.
    await vi.waitFor(() => expect(fetchCalls.some(([url, init]) => url === MAP_URL && method(init) === 'GET')).toBe(true))
    expect(container.querySelector('canvas')).toBeTruthy()
    await vi.waitFor(() => expect(vi.mocked(HTMLCanvasElement.prototype.getContext)).toHaveBeenCalled())

    // Save with nothing edited: nib writes the document it opened, the fixture's exact text.
    const save = await screen.findByRole('button', { name: /^save\b/i })
    await vi.waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false))
    await act(async () => { fireEvent.click(save) })
    await vi.waitFor(() => expect(fetchCalls.some(([, init]) => method(init) === 'POST')).toBe(true))
    const posts = fetchCalls.filter(([, init]) => method(init) === 'POST')
    expect(posts).toHaveLength(1)
    const [postUrl, postInit] = posts[0]
    expect(postUrl).toBe(`/api/revisions/${REVISION}/map`)
    expect(new Headers(postInit?.headers).get('Content-Type')).toMatch(/^application\/json\b/)
    expect(await bodyText(postInit)).toBe(MAP)

    // The server's "unchanged" is shown as a notice, never as an error.
    await vi.waitFor(() => expect(screen.queryAllByRole('status').some(node => /unchanged/i.test(node.textContent ?? ''))).toBe(true))
    expect(screen.queryAllByRole('alert')).toHaveLength(0)
  })

  it('renders no editor for a revision with no map.json', async () => {
    stubBrowser(async url => json({ detail: `unexpected ${url}` }, 404))
    const NibEditor = await realNibEditor()
    const { container } = render(createElement(NibEditor, { revisionId: 'palette-1', mapUrl: null }))
    await act(async () => { await Promise.resolve() })
    expect(container.querySelector('canvas')).toBeNull()
    expect(screen.queryByRole('button', { name: /^save\b/i })).toBeNull()
    expect(fetchCalls).toHaveLength(0)
  })
})

describe('Trace canvas Nodes toggle', () => {
  const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }
  const base: Revision = { id: 'revision-1', project_id: project.id, parent_revision_id: null, kind: 'source', message: null, settings: {}, created_at: '2026-01-01' }
  const flatten: Revision = { ...base, id: 'palette-1', parent_revision_id: 'revision-1', kind: 'palette', settings: { palette: {} }, created_at: '2026-01-02' }
  const trace: Revision = { ...base, id: REVISION, parent_revision_id: 'palette-1', kind: 'trace', created_at: '2026-01-03', settings: { trace: {} } }
  function artifact(kind: string, id: string, revisionId: string, mediaType = 'image/png', filename = `${kind}.png`): Artifact {
    return { id, revision_id: revisionId, kind, filename, media_type: mediaType, byte_size: 1, download_url: `/api/artifacts/${id}/content` }
  }
  const raster = [
    artifact('source', 'source-id', 'revision-1'), artifact('flattened', 'flattened-id', 'palette-1'),
    artifact('segmentation', 'segmentation-id', 'palette-1', 'application/octet-stream', 'segmentation.npz'),
  ]
  const edge = artifact('traced-edge', 'traced-edge-id', REVISION, 'image/svg+xml', 'traced-edge.svg')
  const map = artifact('traced-map', 'map-1', REVISION, 'application/json', 'map.json')

  async function openTrace(artifacts: Artifact[]) {
    stubBrowser(async url => json({ detail: `unexpected ${url}` }, 404))
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [trace, flatten, base] })
    mocks.effectiveArtifacts.mockResolvedValue(artifacts)
    mocks.artifacts.mockResolvedValue([])
    mocks.editor.mockResolvedValue({ width: 400, height: 400, protected_pixels: 0, palette: [] })
    mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
    mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
    mocks.followJob.mockReturnValue(() => undefined)
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=${REVISION}`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(document.querySelector('.artwork-surface img')).toBeTruthy())
    for (const image of document.querySelectorAll<HTMLImageElement>('.artwork-surface img')) {
      Object.defineProperties(image, { naturalWidth: { configurable: true, value: 400 }, naturalHeight: { configurable: true, value: 400 } })
      fireEvent.load(image)
    }
  }
  const nodesToggle = () => {
    const group = document.querySelector<HTMLElement>('.artwork-surface [role="group"][aria-label="Inspection layers"]')
    return group ? within(group).queryByLabelText('Nodes') : null
  }
  // PathNodes' traces: its legend, and its fetch of the traced SVG to read the nodes from.
  const pathNodesTraces = () => ({
    legend: screen.queryByRole('list', { name: 'Node types' }),
    svgFetched: fetchCalls.some(([url]) => url === edge.download_url),
  })

  it('shows nib\'s editor on the shown revision\'s map.json, and no PathNodes overlay', async () => {
    await openTrace([...raster, edge, map])
    await vi.waitFor(() => expect(nodesToggle()).toBeTruthy())
    expect(nibProps.calls).toHaveLength(0)
    await act(async () => { fireEvent.click(nodesToggle()!) })
    await vi.waitFor(() => expect(screen.queryByTestId('nib-editor-stub')).toBeTruthy())
    expect(nibProps.calls.at(-1)).toMatchObject({ revisionId: REVISION, mapUrl: map.download_url })
    expect(pathNodesTraces()).toEqual({ legend: null, svgFetched: false })
  })

  it('shows no nodes overlay for a revision traced before t3 (no map.json)', async () => {
    await openTrace([...raster, edge])
    const toggle = nodesToggle()
    if (toggle) await act(async () => { fireEvent.click(toggle) })
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('nib-editor-stub')).toBeNull()
    expect(nibProps.calls.filter(props => props.mapUrl)).toHaveLength(0)
    expect(pathNodesTraces()).toEqual({ legend: null, svgFetched: false })
  })
})
