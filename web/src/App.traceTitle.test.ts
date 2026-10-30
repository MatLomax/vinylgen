// @vitest-environment jsdom
// The Trace canvas title across a real step, through the real ImageWorkspace (not mocked).
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Project, Revision } from './api'

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(),
  inkscapeHandoff: vi.fn(), health: vi.fn(), followJob: vi.fn(),
}))
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})

const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }
const base: Revision = { id: 'revision-1', project_id: project.id, parent_revision_id: null, kind: 'source', message: null, settings: {}, created_at: '2026-01-01' }
const flatten: Revision = { ...base, id: 'palette-1', kind: 'palette', settings: { palette: {} } }
const solid: Revision = { ...base, id: 'trace-8', parent_revision_id: 'palette-1', kind: 'trace', created_at: '2026-01-02', settings: { trace: { overlap: 0, solid_underlayers: true } } }
const edge: Revision = { ...base, id: 'trace-9', parent_revision_id: 'trace-8', kind: 'trace', created_at: '2026-01-03', settings: { trace: { overlap: 0, solid_underlayers: false } } }

function artifact(kind: string, id: string, revisionId: string, mediaType = 'image/png'): Artifact {
  return { id, revision_id: revisionId, kind, filename: `${kind}.png`, media_type: mediaType, byte_size: 1, download_url: `/artifacts/${id}` }
}
const run = (id: string) => [
  artifact('source', 'source-id', 'revision-1'), artifact('flattened', 'flattened-id', 'palette-1'),
  artifact('segmentation', 'segmentation-id', 'palette-1', 'application/octet-stream'),
  artifact('traced-edge', `${id}-edge`, id, 'image/svg+xml'), artifact('traced-overlap', `${id}-overlap`, id, 'image/svg+xml'),
]

let release: (() => void) | undefined
beforeEach(() => {
  vi.clearAllMocks()
  release = undefined
  localStorage.clear()
  window.scrollTo = vi.fn()
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  mocks.projects.mockResolvedValue([project])
  mocks.project.mockResolvedValue({ ...project, revisions: [edge, solid, flatten, base] })
  mocks.effectiveArtifacts.mockImplementation(async (id: string) => {
    if (id === 'trace-8') await new Promise<void>(resolve => { release = resolve })
    return run(id)
  })
  mocks.artifacts.mockResolvedValue([])
  mocks.editor.mockResolvedValue({ width: 40, height: 30, protected_pixels: 0, palette: [] })
  mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
  mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
  mocks.followJob.mockReturnValue(() => undefined)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('Trace canvas title across a step', () => {
  it('shows only "Loading…" between the previous run\'s name and the new run\'s', async () => {
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-9`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Trace' })
    await within(panel).findByText('Iteration 2 of 2')
    const views = await screen.findAllByRole('group', { name: 'Artwork view' })
    fireEvent.click(within(views[0]).getByRole('button', { name: 'Traced' }))
    const title = () => document.querySelector('.artwork-surface .viewport-toolbar span')
    await vi.waitFor(() => expect(title()?.textContent).toBe('Traced SVG · edge to edge'))

    // Every value the real title takes from here on, including ones replaced within a
    // single task (the observer reports each replaced text).
    const seen: string[] = []
    const observer = new MutationObserver(records => {
      for (const record of records) {
        if (record.type === 'characterData' && record.oldValue !== null) seen.push(record.oldValue)
        for (const node of record.removedNodes) seen.push(node.textContent ?? '')
      }
    })
    observer.observe(title()!, { subtree: true, childList: true, characterData: true, characterDataOldValue: true })
    fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
    await vi.waitFor(() => expect(release).toBeDefined())
    expect(title()?.textContent).toBe('Loading…')
    await act(async () => release!())
    await vi.waitFor(() => expect(title()?.textContent).toBe('Traced SVG · edge to edge'))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    observer.disconnect()
    const sequence = [...seen, title()!.textContent]
    // Every run's edge SVG has one name: the step replaces it with Loading… and then
    // names the stepped-to run's SVG, with nothing else in between.
    expect(sequence[0]).toBe('Traced SVG · edge to edge')
    expect(sequence[1]).toBe('Loading…')
    expect(new Set(sequence.slice(1))).toEqual(new Set(['Loading…', 'Traced SVG · edge to edge']))
  })
})
