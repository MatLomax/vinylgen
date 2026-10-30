// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, FaceEvent, Job, PaletteColour, Project, Revision } from './api'
import type { CanvasLiveTrace } from './ImageWorkspace'
import { paintOrder } from './LiveTrace'

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(),
  inkscapeHandoff: vi.fn(), health: vi.fn(), followJob: vi.fn(), startPipeline: vi.fn(),
}))
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})
type Captured = { canvasLabel?: string; liveTrace?: CanvasLiveTrace; pathNodes?: { url?: string } }
const workspaceProps = vi.hoisted(() => ({ current: null as Captured | null }))
vi.mock('./ImageWorkspace', () => ({ ImageWorkspace: (props: Captured) => { workspaceProps.current = props; return null } }))

const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }

// Pick a project from the header only once its option has rendered: the select is on the
// page before the project list loads, and choosing a value it lacks selects nothing.
async function chooseProject(target: Project = project) {
  await screen.findByRole('option', { name: target.name })
  fireEvent.change(screen.getByLabelText('Active project'), { target: { value: target.id } })
}
const flattenRevision: Revision = { id: 'palette-1', project_id: project.id, parent_revision_id: null, kind: 'palette', message: null, settings: { palette: {} }, created_at: '2026-01-01' }
const palette: PaletteColour[] = [
  { index: 0, name: 'Red', hex: '#ff0000', rgb: [255, 0, 0], share_percent: 40 },
  { index: 1, name: 'Blue', hex: '#0000ff', rgb: [0, 0, 255], share_percent: 35 },
  { index: 2, name: 'White', hex: '#ffffff', rgb: [255, 255, 255], share_percent: 25 },
]

function artifact(kind: string): Artifact {
  return { id: `${kind}-id`, revision_id: flattenRevision.id, kind, filename: `${kind}.png`, media_type: 'image/png', byte_size: 1, download_url: `/artifacts/${kind}` }
}
const job = (status: Job['status']): Job => ({ id: 'job-1', project_id: project.id, revision_id: flattenRevision.id, result_revision_id: status === 'succeeded' ? 'trace-1' : null, kind: 'trace', status, progress: 0, message: null, error: null })
const face = (id: number, label: number): FaceEvent => ({ type: 'face', face: id, label, rings: `M${id} 0L${id + 1} 0L${id} 1Z`, nodes: 'cccc' })

beforeEach(() => {
  vi.clearAllMocks()
  workspaceProps.current = null
  localStorage.clear()
  window.history.replaceState(null, '', '/')
  window.scrollTo = vi.fn()
  mocks.projects.mockResolvedValue([project])
  mocks.project.mockResolvedValue({ ...project, revisions: [flattenRevision] })
  mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened'), artifact('segmentation')])
  mocks.artifacts.mockResolvedValue([])
  mocks.editor.mockResolvedValue({ width: 1200, height: 900, protected_pixels: 0, palette, removed_colour_index: 2 })
  mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
  mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
  mocks.followJob.mockReturnValue(() => undefined)
  mocks.startPipeline.mockResolvedValue(job('running'))
})
afterEach(cleanup)

describe('Trace canvas live trace wiring', () => {
  it('draws the running trace\'s faces as they land, in paint order and the revision\'s colours, only on Trace', async () => {
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(screen.getByRole('tab', { name: 'Trace' }))
    const panel = screen.getByRole('tabpanel', { name: 'Trace' })
    const run = within(panel).getByRole('button', { name: 'Trace and validate' })
    await vi.waitFor(() => expect((run as HTMLButtonElement).disabled).toBe(false))
    expect(workspaceProps.current?.liveTrace).toBeUndefined()
    fireEvent.click(run)
    await vi.waitFor(() => expect(mocks.followJob).toHaveBeenCalled())
    expect(mocks.startPipeline.mock.calls[0][3]).toEqual({ trace: { soft_path_nodes: 5000, soft_layer_nodes: 50000, soft_document_nodes: 200000 } })
    const [, onJob, , onFace] = mocks.followJob.mock.calls[0] as [string, (job: Job) => void, unknown, (face: FaceEvent) => void]

    // t3's emission order, never sorted on arrival: a later face may paint lower.
    act(() => { onFace(face(7, 1)); onFace(face(3, 0)); onFace(face(5, 2)); onFace(face(1, 1)) })
    const live = workspaceProps.current?.liveTrace
    expect(live?.traced).toEqual({ width: 1200, height: 900 })
    expect(live?.omitted).toBe(2)
    const layers = paintOrder(live!.faces, live!.palette, live!.omitted)
    // Palette order bottom to top, each layer's faces in face order; the omitted White has no layer.
    expect(layers.map(layer => [layer.colour.hex, layer.faces.map(row => row.face)])).toEqual([['#ff0000', [3]], ['#0000ff', [1, 7]]])

    fireEvent.click(screen.getByRole('tab', { name: 'Edit' }))
    await vi.waitFor(() => expect(workspaceProps.current?.canvasLabel).toBe('Palette canvas'))
    expect(workspaceProps.current?.liveTrace).toBeUndefined()
    fireEvent.click(screen.getByRole('tab', { name: 'Trace' }))
    await vi.waitFor(() => expect(workspaceProps.current?.liveTrace).toBeDefined())

    // The finished job's faces are dropped: its edge SVG shows the result.
    act(() => onJob(job('failed')))
    await vi.waitFor(() => expect(workspaceProps.current?.liveTrace).toBeUndefined())
  })
})

describe('Trace canvas Nodes overlay wiring', () => {
  it('gives the Trace canvas the traced SVG it shows, and no other canvas', async () => {
    const traced: Artifact[] = ['traced-edge', 'traced-overlap'].map(kind => ({ ...artifact(kind), filename: `${kind}.svg`, media_type: 'image/svg+xml', download_url: `/artifacts/${kind}.svg` }))
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened'), ...traced])
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(screen.getByRole('tab', { name: 'Trace' }))
    // The edge SVG is the one shown, whatever else an old run wrote.
    await vi.waitFor(() => expect(workspaceProps.current?.pathNodes).toEqual({ url: '/artifacts/traced-edge.svg' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    await vi.waitFor(() => expect(workspaceProps.current?.canvasLabel).toBe('Flatten canvas'))
    expect(workspaceProps.current?.pathNodes).toBeUndefined()
  })
})
