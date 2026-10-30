// @vitest-environment jsdom
// Card 8 of vinylgen-on-t3 (tracer `cards/vinylgen-on-t3/build-8a-brief.md`): the outcomes of a save
// from nib's chrome. The chrome's Save calls the host's `save`, which posts the map; the outcome
// is the chrome's `notice(text)`:
// - 201: "Saved as a new trace revision.", and the new revision goes to `onSaved`;
// - 422 "unchanged": the server's detail (NibEditor.test.tsx);
// - any other refusal: the server's detail followed by its problems, one per sentence (each
//   problem with its first letter capitalised and a closing period unless it already ends in
//   one of `.`, `!` or `?`), joined by single spaces.
// On the Nodes stage the saved child trace revision ("edited in nib") is selected, as
// `mapSaved` does, and the editor opens the child's own map.json.
//
// `mountChrome` is a stub (the 0.2.0 tarball is not vendored yet) recording its notices and
// offering a Save button that calls `editor.saveDocument()`, as nib's chrome does; the installed
// package's real `mountEditor` runs on inert canvas stubs (nib's `dom.testkit.ts`).
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Project, Revision } from './api'
import { NibEditor } from './NibEditor'

type ChromeCall = { container: HTMLElement; editor: unknown; options: unknown; root: HTMLElement; save: HTMLButtonElement; notices: (string | null)[] }
type Mounted = { editor: unknown; loaded: Promise<unknown>; dispose(): void }
type EditorCall = { canvas: HTMLCanvasElement; options: { text?: string; kind?: string }; mounted: Mounted }

const nibCore = vi.hoisted(() => ({ chromes: [] as ChromeCall[], editors: [] as EditorCall[] }))
vi.mock('@nib/core', async importOriginal => {
  const original = await importOriginal<Record<string, unknown>>()
  const mountEditor = original.mountEditor as (canvas: HTMLCanvasElement, options: EditorCall['options']) => Mounted
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
      const call: ChromeCall = { container, editor, options, root, save, notices: [] }
      nibCore.chromes.push(call)
      return { root, notice: (text: string | null) => { call.notices.push(text) }, dispose: () => { root.remove() } }
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

const NODE_FS = 'node:fs'
let MAP = ''
beforeAll(async () => {
  const fs = await import(/* @vite-ignore */ NODE_FS) as { readFileSync(path: string, encoding: 'utf8'): string }
  const here = (import.meta as unknown as { dirname: string }).dirname
  MAP = fs.readFileSync(`${here}/../../tests/fixtures/t3-test-array/map.json`, 'utf8')
})
const MAP_URL = '/api/artifacts/map-1/content'
const noop = () => {}
const inert = () => new Proxy({} as Record<PropertyKey, unknown>, {
  get: (target, key) => (key in target ? target[key] : key === 'isPointInPath' ? () => false : noop),
  set: (target, key, value) => { target[key] = value; return true },
})
const json = (body: unknown, status: number) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
let posts: string[] = []

function stubBrowser(answer: () => Response) {
  posts = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (String(url).startsWith('/api/artifacts/map-') && (init?.method ?? 'GET') === 'GET') return new Response(MAP, { status: 200 })
    if (url === '/api/revisions/trace-1/map' && init?.method === 'POST') { posts.push(String(url)); return answer() }
    return json({ detail: 'unexpected' }, 404)
  }))
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('requestAnimationFrame', () => 1)
  vi.stubGlobal('cancelAnimationFrame', noop)
  vi.stubGlobal('Path2D', class { constructor() { return inert() } })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => inert() as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) }))
}

// Wait for nib's editor (its map loaded) and chrome, then press the chrome's Save.
async function pressSave(index = 0) {
  await vi.waitFor(() => expect(nibCore.editors.length).toBeGreaterThan(index))
  await nibCore.editors[index].mounted.loaded
  await vi.waitFor(() => expect(nibCore.chromes.length).toBeGreaterThan(index))
  const chrome = nibCore.chromes[index]
  await act(async () => { fireEvent.click(chrome.save) })
  await vi.waitFor(() => expect(posts).toHaveLength(1))
  return chrome
}

async function saveWith(answer: Response, onSaved = vi.fn()) {
  stubBrowser(() => answer)
  const { container } = render(createElement(NibEditor, { revisionId: 'trace-1', mapUrl: MAP_URL, onSaved }))
  const chrome = await pressSave()
  return { onSaved, chrome, container }
}
const ownNotices = (container: HTMLElement, chrome: ChromeCall) =>
  [...container.querySelectorAll('[role="status"], [role="alert"]')].filter(element => !chrome.root.contains(element))

beforeEach(() => {
  vi.clearAllMocks()
  nibCore.chromes = []
  nibCore.editors = []
  localStorage.clear()
  window.history.replaceState(null, '', '/')
  window.scrollTo = vi.fn()
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear() })

describe('NibEditor saves', () => {
  it('hands the new revision to onSaved and says so on the chrome\'s notice line', async () => {
    const child: Revision = { id: 'child-1', project_id: 'p', parent_revision_id: 'trace-1', kind: 'trace', message: 'edited in nib', settings: {}, created_at: '2026-01-01' }
    const { onSaved, chrome, container } = await saveWith(json(child, 201))
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledWith(child))
    await vi.waitFor(() => expect(chrome.notices.at(-1)).toBe('Saved as a new trace revision.'))
    expect(ownNotices(container, chrome)).toHaveLength(0)
  })

  it('puts any other refusal on the chrome\'s notice line: its detail, then its problems one per sentence', async () => {
    const refusal = {
      detail: 'The map fails the planar-map check with 2 violation(s).', reason: 'invalid', violations: 2,
      problems: ['ring 0 of face 0 does not close', 'edge 3 sides disagree with its references.'],
    }
    const { onSaved, chrome, container } = await saveWith(json(refusal, 422))
    await vi.waitFor(() => expect(chrome.notices.filter(Boolean)).not.toHaveLength(0))
    expect(chrome.notices.at(-1)).toBe(
      'The map fails the planar-map check with 2 violation(s). Ring 0 of face 0 does not close. Edge 3 sides disagree with its references.')
    expect(onSaved).not.toHaveBeenCalled()
    expect(ownNotices(container, chrome)).toHaveLength(0)
  })

  it('puts a mismatch refusal on the notice line the same way', async () => {
    const refusal = { detail: 'The map is not this trace\'s: its size or palette differ.', reason: 'mismatch', problems: ['W is 101, the parent\'s is 100'] }
    const { onSaved, chrome } = await saveWith(json(refusal, 422))
    await vi.waitFor(() => expect(chrome.notices.at(-1)).toBe('The map is not this trace\'s: its size or palette differ. W is 101, the parent\'s is 100.'))
    expect(onSaved).not.toHaveBeenCalled()
  })
})

describe('A save on the Nodes stage', () => {
  const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }
  const base: Revision = { id: 'revision-1', project_id: project.id, parent_revision_id: null, kind: 'source', message: null, settings: {}, created_at: '2026-01-01' }
  const flatten: Revision = { ...base, id: 'palette-1', parent_revision_id: 'revision-1', kind: 'palette', settings: { palette: {} }, created_at: '2026-01-02' }
  const trace: Revision = { ...base, id: 'trace-1', parent_revision_id: 'palette-1', kind: 'trace', settings: { trace: {} }, created_at: '2026-01-03' }
  const child: Revision = { ...trace, id: 'child-1', parent_revision_id: 'trace-1', message: 'edited in nib', created_at: '2026-01-04' }
  function artifact(kind: string, id: string, revisionId: string, mediaType = 'image/png', filename = `${kind}.png`): Artifact {
    return { id, revision_id: revisionId, kind, filename, media_type: mediaType, byte_size: 1, download_url: `/api/artifacts/${id}/content` }
  }
  const raster = [
    artifact('source', 'source-id', 'revision-1'), artifact('flattened', 'flattened-id', 'palette-1'),
    artifact('segmentation', 'segmentation-id', 'palette-1', 'application/octet-stream', 'segmentation.npz'),
  ]
  const own = (revisionId: string) => [
    artifact('traced-edge', `edge-${revisionId}`, revisionId, 'image/svg+xml', 'traced-edge.svg'),
    artifact('traced-map', `map-${revisionId}`, revisionId, 'application/json', 'map.json'),
  ]

  it('selects the child trace revision the save made, and opens nib\'s editor on its own map.json', async () => {
    stubBrowser(() => json(child, 201))
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [trace, flatten, base] })
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => [...raster, ...own(id === 'child-1' ? 'child-1' : 'trace-1')])
    mocks.artifacts.mockResolvedValue([])
    mocks.editor.mockResolvedValue({ width: 400, height: 400, protected_pixels: 0, palette: [] })
    mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
    mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
    mocks.followJob.mockReturnValue(() => undefined)
    window.history.replaceState(null, '', `/#stage=nodes&project=${project.id}&revision=trace-1`)
    render(createElement((await import('./App')).default))
    await screen.findByRole('tab', { name: 'Nodes' })

    // The project lists the child once it exists.
    mocks.project.mockResolvedValue({ ...project, revisions: [child, trace, flatten, base] })
    const chrome = await pressSave()
    expect(posts).toEqual(['/api/revisions/trace-1/map'])
    await vi.waitFor(() => expect(chrome.notices).toContain('Saved as a new trace revision.'))
    await vi.waitFor(() => expect(new URLSearchParams(window.location.hash.slice(1)).get('revision')).toBe('child-1'))
    expect(new URLSearchParams(window.location.hash.slice(1)).get('stage')).toBe('nodes')
    await vi.waitFor(() => expect(mocks.effectiveArtifacts.mock.calls.some(([id]) => id === 'child-1')).toBe(true))
    // The editor opens the child's own map.
    await vi.waitFor(() => expect(nibCore.editors.length).toBeGreaterThan(1))
    expect(vi.mocked(fetch).mock.calls.some(([url]) => url === '/api/artifacts/map-child-1/content')).toBe(true)
    expect((screen.getByLabelText('Revision') as HTMLSelectElement).value).toBe('child-1')
  })
})
