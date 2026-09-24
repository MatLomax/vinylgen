// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Project, Revision } from './api'
import { DEFAULT_CLEANUP_SETTINGS } from './PipelineControls'
import type { CanvasCleanupPreview, CanvasLivePreview } from './ImageWorkspace'

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(),
  inkscapeHandoff: vi.fn(), health: vi.fn(), followJob: vi.fn(),
}))
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})
// The canvas gets the options at once, with a null grid (paused) until it is measured.
type Captured = { canvasLabel?: string; livePreview?: CanvasLivePreview; cleanupPreview?: CanvasCleanupPreview; livePreviewPaused?: boolean; inspectionKinds?: readonly string[]; layers?: { kind?: string; role: string; name: string }[] }
const workspaceProps = vi.hoisted(() => ({ current: null as Captured | null, history: [] as Captured[] }))
vi.mock('./ImageWorkspace', () => ({ ImageWorkspace: (props: Captured) => { workspaceProps.current = props; workspaceProps.history.push(props); return null } }))

const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }

// Pick a project from the header only once its option has rendered: the select is on the
// page before the project list loads, and choosing a value it lacks selects nothing.
async function chooseProject(target: Project = project) {
  await screen.findByRole('option', { name: target.name })
  fireEvent.change(screen.getByLabelText('Active project'), { target: { value: target.id } })
}
const flattenRevision: Revision = { id: 'palette-1', project_id: project.id, parent_revision_id: null, kind: 'palette', message: null, settings: { palette: {} }, created_at: '2026-01-01' }

function artifact(kind: string): Artifact {
  return { id: `${kind}-id`, revision_id: flattenRevision.id, kind, filename: `${kind}.png`, media_type: 'image/png', byte_size: 1, download_url: `/artifacts/${kind}` }
}

beforeEach(() => {
  vi.clearAllMocks()
  workspaceProps.current = null
  workspaceProps.history = []
  localStorage.clear()
  window.history.replaceState(null, '', '/')
  window.scrollTo = vi.fn()
  mocks.projects.mockResolvedValue([project])
  mocks.project.mockResolvedValue({ ...project, revisions: [flattenRevision] })
  mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened')])
  mocks.artifacts.mockResolvedValue([])
  mocks.editor.mockResolvedValue({ width: 1200, height: 900, protected_pixels: 0, palette: [] })
  mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
  mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
  mocks.followJob.mockReturnValue(() => undefined)
})
afterEach(cleanup)

describe('Cleanup canvas live preview wiring', () => {
  it('previews the active revision with the unsaved Cleanup form, size included, only on Cleanup', async () => {
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(screen.getByRole('tab', { name: 'Cleanup' }))
    await vi.waitFor(() => expect(workspaceProps.current?.livePreviewPaused).toBe(false))
    expect(workspaceProps.current?.canvasLabel).toBe('Cleanup canvas')
    expect(workspaceProps.current?.livePreview).toBeUndefined()
    expect(workspaceProps.current?.cleanupPreview).toEqual({ revisionId: flattenRevision.id, settings: DEFAULT_CLEANUP_SETTINGS, tracedSize: { width: 1200, height: 900 }, changesUnavailable: 'No Cleanup changes to show' })
    const cleanupProps = workspaceProps.history.filter(props => props.cleanupPreview)
    // Never running without its grid.
    expect(cleanupProps.every(props => props.livePreviewPaused || props.cleanupPreview?.tracedSize)).toBe(true)

    // Typing into the form re-previews with the edited values, without saving or running.
    const panel = screen.getByRole('tabpanel', { name: 'Cleanup' })
    const width = within(panel).getByLabelText(/Width \(mm\)/) as HTMLInputElement
    fireEvent.focus(width)
    fireEvent.change(width, { target: { value: '300' } })
    fireEvent.click(within(panel).getByLabelText(/Enable cutter cleanup/))
    const area = within(panel).getByLabelText(/Minimum region area/) as HTMLInputElement
    fireEvent.focus(area)
    fireEvent.change(area, { target: { value: '2.5' } })
    expect(workspaceProps.current?.cleanupPreview?.settings).toMatchObject({ width_mm: 300, cleanup: true, min_region_area: 2.5 })
    expect(mocks.followJob).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('tab', { name: 'Trace' }))
    await vi.waitFor(() => expect(workspaceProps.current?.canvasLabel).toBe('Trace canvas'))
    expect(workspaceProps.current?.cleanupPreview).toBeUndefined()
    expect(workspaceProps.current?.livePreview).toBeTruthy()
  })
})

describe('Cleanup canvas applied changes', () => {
  const cleanupRevision: Revision = { id: 'cleanup-1', project_id: project.id, parent_revision_id: flattenRevision.id, kind: 'cleanup', message: null, settings: { cleanup: { ...DEFAULT_CLEANUP_SETTINGS, cleanup: true, min_region_area: 4 } }, created_at: '2026-01-02' }
  const cleaned = (kind: string): Artifact => ({ ...artifact(kind), id: `${kind}-cleaned`, revision_id: cleanupRevision.id })

  async function openCleanup() {
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(screen.getByRole('tab', { name: 'Cleanup' }))
    await vi.waitFor(() => expect(workspaceProps.current?.livePreviewPaused).toBe(false))
  }

  it('offers the cleanup run\'s changes overlay on the Cleanup canvas', async () => {
    mocks.project.mockResolvedValue({ ...project, revisions: [cleanupRevision, flattenRevision] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), cleaned('flattened'), cleaned('cleanup-changes-overlay')])
    await openCleanup()
    expect(workspaceProps.current?.inspectionKinds).toEqual(['cleanup-changes-overlay'])
    expect(workspaceProps.current?.layers?.find(layer => layer.kind === 'cleanup-changes-overlay')).toMatchObject({ role: 'overlay', name: 'Cleanup changes (removed red, widened green)' })
  })

  it('asks for a re-run when the cleanup changed pixels but recorded no overlay', async () => {
    mocks.project.mockResolvedValue({ ...project, revisions: [cleanupRevision, flattenRevision] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), cleaned('flattened')])
    await openCleanup()
    await vi.waitFor(() => expect(workspaceProps.current?.cleanupPreview?.changesUnavailable).toBe('Re-run Cleanup to record its changes'))
  })
})

