// @vitest-environment jsdom
// Card 8 of vinylgen-on-t3 (tracer `cards/vinylgen-on-t3/build-8a-brief.md`): the outcomes of a save
// from nib's chrome. The chrome's Save calls the host's `save`, which posts the map; the outcome
// is the chrome's `notice(text, tone)`:
// - 201: "Saved as a new trace revision." in the success tone (nib core 0.12.1's green toast), and
//   the new revision goes to `onSaved`;
// - 422 "unchanged": the server's detail (NibEditor.test.tsx);
// - any other refusal: the server's detail followed by its problems, one per sentence (each
//   problem with its first letter capitalised and a closing period unless it already ends in
//   one of `.`, `!` or `?`), joined by single spaces, in the error tone.
// The `notice` prop is shown in the success tone when it is that saved line, and as info otherwise.
// On the Nodes stage the saved child trace revision ("edited in nib") is selected, as
// `mapSaved` does. Card 24 (`build-24-brief.md`): the save keeps the editor as it is. The posted
// text is the new revision's map byte for byte, so the editor is neither disposed nor remounted,
// across `mapSaved`, the new revision's artefacts loading (or failing to load) and the props
// moving onto the child's own map.json; the next save posts to the child, and the editor reads
// saved (`onEditedChange(false)`) with its undo history kept. Opening another revision remounts.
//
// `mountChrome` is a stub recording its notices and
// offering a Save button that calls `editor.saveDocument()`, as nib's chrome does; the installed
// package's real `mountEditor` runs on inert canvas stubs (nib's `dom.testkit.ts`).
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Project, Revision } from './api'
import { NibEditor, SAVED } from './NibEditor'

type ChromeCall = { container: HTMLElement; editor: unknown; options: unknown; root: HTMLElement; save: HTMLButtonElement; notices: (string | null)[]; tones: (string | undefined)[] }
type Mounted = { editor: unknown; loaded: Promise<unknown>; dispose(): void }
type EditorCall = { canvas: HTMLCanvasElement; options: { text?: string; kind?: string }; mounted: Mounted; disposed: number }
// The few editor members the card-24 tests drive: an edit (lock a face), undo, and the stats.
type Driven = { setNodeLocked(id: string, on: boolean): void; undo(): void; onStats(fn: (stats: { unsaved?: boolean; canUndo: boolean }) => void): () => void; doc: { paths: { id: string }[] } }

const nibCore = vi.hoisted(() => ({ chromes: [] as ChromeCall[], editors: [] as EditorCall[] }))
vi.mock('@nib/core', async importOriginal => {
  const original = await importOriginal<Record<string, unknown>>()
  const mountEditor = original.mountEditor as (canvas: HTMLCanvasElement, options: EditorCall['options']) => Mounted
  return {
    ...original,
    mountEditor: vi.fn((canvas: HTMLCanvasElement, options: EditorCall['options']) => {
      const mounted = mountEditor(canvas, options)
      const call: EditorCall = { canvas, options, mounted, disposed: 0 }
      nibCore.editors.push(call)
      const dispose = mounted.dispose.bind(mounted)
      mounted.dispose = () => { call.disposed += 1; dispose() }
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
      const call: ChromeCall = { container, editor, options, root, save, notices: [], tones: [] }
      nibCore.chromes.push(call)
      return { root, notice: (text: string | null, tone?: string) => { call.notices.push(text); call.tones.push(tone) }, dispose: () => { root.remove() } }
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
    if (/^\/api\/revisions\/[^/]+\/map$/.test(String(url)) && init?.method === 'POST') { posts.push(String(url)); return answer() }
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
async function pressSave(index = 0, count = 1) {
  await vi.waitFor(() => expect(nibCore.editors.length).toBeGreaterThan(index))
  await nibCore.editors[index].mounted.loaded
  await vi.waitFor(() => expect(nibCore.chromes.length).toBeGreaterThan(index))
  const chrome = nibCore.chromes[index]
  await act(async () => { fireEvent.click(chrome.save) })
  await vi.waitFor(() => expect(posts).toHaveLength(count))
  return chrome
}

// The mounted editor's members the tests drive.
const driven = (index = 0) => nibCore.editors[index].mounted.editor as Driven
// One frame's stats emission: the stubbed requestAnimationFrame never runs nib's render loop.
const frame = (index = 0) => (driven(index) as unknown as { emitStats(fps: number): void }).emitStats(0)
// An edit in the open map: toggle its first face's lock, one undoable command, then a frame.
async function edit(index = 0) {
  await nibCore.editors[index].mounted.loaded
  await act(async () => {
    const editor = driven(index), id = editor.doc.paths[0].id
    const locked = (editor as unknown as { doc: { paths: { locked?: boolean }[] } }).doc.paths[0].locked === true
    editor.setNodeLocked(id, !locked)
    frame(index)
  })
}
// The editor's stats as nib emits them on its next frame.
function statsOf(index = 0) {
  let seen: { unsaved?: boolean; canUndo: boolean } | null = null
  const stop = driven(index).onStats(stats => { seen = stats })
  frame(index)
  stop()
  return seen as unknown as { unsaved?: boolean; canUndo: boolean }
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
    expect(chrome.tones.at(-1)).toBe('success')
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
    expect(chrome.tones.at(-1)).toBe('error')
    expect(onSaved).not.toHaveBeenCalled()
    expect(ownNotices(container, chrome)).toHaveLength(0)
  })

  it('puts a mismatch refusal on the notice line the same way', async () => {
    const refusal = { detail: 'The map is not this trace\'s: its size or palette differ.', reason: 'mismatch', problems: ['W is 101, the parent\'s is 100'] }
    const { onSaved, chrome } = await saveWith(json(refusal, 422))
    await vi.waitFor(() => expect(chrome.notices.at(-1)).toBe('The map is not this trace\'s: its size or palette differ. W is 101, the parent\'s is 100.'))
    expect(chrome.tones.at(-1)).toBe('error')
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('puts a failed post on the notice line in the error tone', async () => {
    stubBrowser(() => { throw new Error('offline') })
    const onSaved = vi.fn()
    render(createElement(NibEditor, { revisionId: 'trace-1', mapUrl: MAP_URL, onSaved }))
    const chrome = await pressSave()
    await vi.waitFor(() => expect(chrome.notices.at(-1)).toMatch(/^Could not save the map: /))
    expect(chrome.tones.at(-1)).toBe('error')
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('shows the notice prop in the success tone when it is the saved line, and as info otherwise', async () => {
    stubBrowser(() => json({}, 200))
    const view = render(createElement(NibEditor, { revisionId: 'trace-1', mapUrl: MAP_URL, notice: SAVED }))
    await vi.waitFor(() => expect(nibCore.chromes).toHaveLength(1))
    const chrome = nibCore.chromes[0]
    await vi.waitFor(() => expect(chrome.notices).toContain(SAVED))
    expect(chrome.tones[chrome.notices.indexOf(SAVED)]).toBe('success')
    view.rerender(createElement(NibEditor, { revisionId: 'trace-1', mapUrl: MAP_URL, notice: 'Traced with the new setting.' }))
    await vi.waitFor(() => expect(chrome.notices.at(-1)).toBe('Traced with the new setting.'))
    expect(chrome.tones.at(-1)).toBe('info')
  })
})

describe('A save keeps the editor as it is', () => {
  const child: Revision = { id: 'child-1', project_id: 'p', parent_revision_id: 'trace-1', kind: 'trace', message: 'edited in nib', settings: {}, created_at: '2026-01-01' }
  const grandchild: Revision = { ...child, id: 'child-2', parent_revision_id: 'child-1' }

  it('after a 201 the editor is neither disposed nor remounted, even when the props move to the saved revision\'s map', async () => {
    const answers = [json(child, 201), json(grandchild, 201)]
    stubBrowser(() => answers.shift()!)
    const onSaved = vi.fn(), onEditedChange = vi.fn()
    const view = render(createElement(NibEditor, { revisionId: 'trace-1', mapUrl: MAP_URL, onSaved, onEditedChange }))
    await vi.waitFor(() => expect(nibCore.editors).toHaveLength(1))
    await edit()
    await vi.waitFor(() => expect(onEditedChange.mock.calls).toEqual([[true]]))
    await pressSave()
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledWith(child))
    // The saved revision's own map arrives as the new props.
    view.rerender(createElement(NibEditor, { revisionId: 'child-1', mapUrl: '/api/artifacts/map-child-1/content', onSaved, onEditedChange }))
    await act(async () => { await Promise.resolve() })
    expect(nibCore.editors).toHaveLength(1)
    expect(nibCore.editors[0].disposed).toBe(0)
    expect(nibCore.chromes).toHaveLength(1)
    // The editor reads saved, and its undo history is kept.
    await vi.waitFor(() => expect(onEditedChange.mock.calls).toEqual([[true], [false]]))
    expect(statsOf()).toMatchObject({ unsaved: false, canUndo: true })
    // The next save posts to the revision the last save made.
    await edit()
    await pressSave(0, 2)
    expect(posts).toEqual(['/api/revisions/trace-1/map', '/api/revisions/child-1/map'])
  })

  it('the next save posts to the saved revision even before its props arrive', async () => {
    const answers = [json(child, 201), json(grandchild, 201)]
    stubBrowser(() => answers.shift()!)
    render(createElement(NibEditor, { revisionId: 'trace-1', mapUrl: MAP_URL }))
    await vi.waitFor(() => expect(nibCore.editors).toHaveLength(1))
    await edit()
    await pressSave()
    await edit()
    await pressSave(0, 2)
    expect(posts).toEqual(['/api/revisions/trace-1/map', '/api/revisions/child-1/map'])
  })

  it('undo after the save undoes the edit and the editor reads unsaved again', async () => {
    stubBrowser(() => json(child, 201))
    const onEditedChange = vi.fn()
    render(createElement(NibEditor, { revisionId: 'trace-1', mapUrl: MAP_URL, onEditedChange }))
    await vi.waitFor(() => expect(nibCore.editors).toHaveLength(1))
    await edit()
    await pressSave()
    await vi.waitFor(() => expect(onEditedChange.mock.calls).toEqual([[true], [false]]))
    await act(async () => { driven().undo(); frame() })
    expect(driven().doc.paths.length).toBeGreaterThan(0)
    expect(statsOf()).toMatchObject({ unsaved: true, canUndo: false })
    expect(onEditedChange.mock.calls).toEqual([[true], [false], [true]])
  })

  it('another map that is not the saved revision\'s still remounts', async () => {
    stubBrowser(() => json(child, 201))
    const view = render(createElement(NibEditor, { revisionId: 'trace-1', mapUrl: MAP_URL }))
    await vi.waitFor(() => expect(nibCore.editors).toHaveLength(1))
    await edit()
    await pressSave()
    view.rerender(createElement(NibEditor, { revisionId: 'trace-9', mapUrl: '/api/artifacts/map-trace-9/content' }))
    await vi.waitFor(() => expect(nibCore.editors).toHaveLength(2))
    expect(nibCore.editors[0].disposed).toBe(1)
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
    expect((screen.getByLabelText('Revision') as HTMLSelectElement).value).toBe('child-1')
  })

  // The App on the Nodes stage, with the child's artefacts held until `release` is called.
  async function nodesStage(childArtifacts: () => Promise<Artifact[]>) {
    stubBrowser(() => json(child, 201))
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [trace, flatten, base] })
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => id === 'child-1' ? childArtifacts() : [...raster, ...own('trace-1')])
    mocks.artifacts.mockResolvedValue([])
    mocks.editor.mockResolvedValue({ width: 400, height: 400, protected_pixels: 0, palette: [] })
    mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
    mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
    mocks.followJob.mockReturnValue(() => undefined)
    window.history.replaceState(null, '', `/#stage=nodes&project=${project.id}&revision=trace-1`)
    render(createElement((await import('./App')).default))
    await screen.findByRole('tab', { name: 'Nodes' })
    mocks.project.mockResolvedValue({ ...project, revisions: [child, trace, flatten, base] })
    await vi.waitFor(() => expect(nibCore.editors).toHaveLength(1))
    await edit()
    const chrome = await pressSave()
    await vi.waitFor(() => expect(new URLSearchParams(window.location.hash.slice(1)).get('revision')).toBe('child-1'))
    await vi.waitFor(() => expect(mocks.effectiveArtifacts.mock.calls.some(([id]) => id === 'child-1')).toBe(true))
    return chrome
  }

  it('keeps the same NibEditor across mapSaved and the saved revision\'s artefacts loading', async () => {
    let release: (rows: Artifact[]) => void = () => {}
    await nodesStage(() => new Promise(resolve => { release = resolve }))
    // While the child's artefacts load, the editor stays on the map it holds.
    expect(document.querySelector('.nodes-unavailable')).toBeNull()
    expect(nibCore.editors).toHaveLength(1)
    expect(nibCore.editors[0].disposed).toBe(0)
    await act(async () => { release([...raster, ...own('child-1')]) })
    await act(async () => { await Promise.resolve() })
    expect(document.querySelector('.nodes-unavailable')).toBeNull()
    expect(nibCore.editors).toHaveLength(1)
    expect(nibCore.editors[0].disposed).toBe(0)
    expect(statsOf()).toMatchObject({ unsaved: false, canUndo: true })
    // The run switcher is not locked.
    expect(screen.queryByText('Save or undo your node edits to open another trace run.')).toBeNull()
    // The next save posts to the child.
    await edit()
    await pressSave(0, 2)
    expect(posts).toEqual(['/api/revisions/trace-1/map', '/api/revisions/child-1/map'])
  })

  it('keeps the editor and shows the error when the saved revision\'s artefacts fail to load', async () => {
    await nodesStage(async () => { throw new Error('the artefacts are gone') })
    await vi.waitFor(() => expect(screen.getByRole('alert').textContent).toContain('the artefacts are gone'))
    expect(nibCore.editors).toHaveLength(1)
    expect(nibCore.editors[0].disposed).toBe(0)
    expect(statsOf()).toMatchObject({ unsaved: false, canUndo: true })
  })

  it('opening another revision after the save still remounts the editor', async () => {
    let release: (rows: Artifact[]) => void = () => {}
    await nodesStage(() => new Promise(resolve => { release = resolve }))
    await act(async () => { release([...raster, ...own('child-1')]) })
    await act(async () => { fireEvent.change(screen.getByLabelText('Revision'), { target: { value: 'trace-1' } }) })
    await vi.waitFor(() => expect(nibCore.editors).toHaveLength(2))
    expect(nibCore.editors[0].disposed).toBe(1)
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/artifacts/map-trace-1/content')).toHaveLength(2)
  })
})
