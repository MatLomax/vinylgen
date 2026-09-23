// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Project, Revision } from './api'
import { DEFAULT_TRACE_SETTINGS } from './PipelineControls'
import type { LivePreviewOptions } from './TracePreview'

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(),
  inkscapeHandoff: vi.fn(), health: vi.fn(), followJob: vi.fn(),
}))
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})
type Captured = { canvasLabel?: string; livePreview?: LivePreviewOptions }
const workspaceProps = vi.hoisted(() => ({ current: null as Captured | null }))
vi.mock('./ImageWorkspace', () => ({ ImageWorkspace: (props: Captured) => { workspaceProps.current = props; return null } }))

const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }
const flattenRevision: Revision = { id: 'palette-1', project_id: project.id, parent_revision_id: null, kind: 'palette', message: null, settings: { palette: {} }, created_at: '2026-01-01' }

function artifact(kind: string): Artifact {
  return { id: `${kind}-id`, revision_id: flattenRevision.id, kind, filename: `${kind}.png`, media_type: 'image/png', byte_size: 1, download_url: `/artifacts/${kind}` }
}

beforeEach(() => {
  vi.clearAllMocks()
  workspaceProps.current = null
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

describe('Trace canvas live preview wiring', () => {
  it('previews the active revision with the unsaved trace form on the traced grid, only on Trace', async () => {
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })
    fireEvent.click(screen.getByRole('tab', { name: 'Trace' }))
    await vi.waitFor(() => expect(workspaceProps.current?.livePreview).toBeTruthy())
    expect(workspaceProps.current?.canvasLabel).toBe('Trace canvas')
    expect(workspaceProps.current?.livePreview).toEqual({ revisionId: flattenRevision.id, settings: DEFAULT_TRACE_SETTINGS, tracedSize: { width: 1200, height: 900 } })

    // Typing into a field re-previews with the edited value, without waiting for blur.
    const panel = screen.getByRole('tabpanel', { name: 'Trace' })
    const field = within(panel).getByLabelText(/Curve-fit tolerance/) as HTMLInputElement
    fireEvent.focus(field)
    fireEvent.change(field, { target: { value: '0.7' } })
    expect(workspaceProps.current?.livePreview?.settings.curve_fit_tolerance).toBe(0.7)

    fireEvent.click(screen.getByRole('tab', { name: 'Edit' }))
    await vi.waitFor(() => expect(workspaceProps.current?.canvasLabel).toBe('Palette canvas'))
    expect(workspaceProps.current?.livePreview).toBeUndefined()
  })
})
