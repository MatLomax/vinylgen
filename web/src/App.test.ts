// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Job, Project, Revision } from './api'
import { artifactToWorkspaceLayer } from './App'

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(),
  generate: vi.fn(), inkscapeHandoff: vi.fn(), saveForInkscape: vi.fn(),
  followJob: vi.fn(),
}))

vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})
vi.mock('./ImageWorkspace', () => ({ ImageWorkspace: () => null }))
vi.mock('./FlattenedEditor', () => ({ FlattenedEditor: () => null }))
vi.mock('./MaskedEditor', () => ({ MaskedEditor: () => null }))

const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }
const revision: Revision = { id: 'revision-1', project_id: project.id, parent_revision_id: null, kind: 'source', message: null, settings: {}, created_at: '2026-01-01' }
const queuedJob: Job = { id: 'job-1', project_id: project.id, revision_id: revision.id, result_revision_id: null, kind: 'generate', status: 'queued', progress: 0.25, message: 'Preparing artwork', error: null }

beforeEach(() => {
  vi.clearAllMocks()
  mocks.projects.mockResolvedValue([])
  mocks.project.mockResolvedValue({ ...project, revisions: [revision] })
  mocks.effectiveArtifacts.mockResolvedValue([])
  mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
  mocks.followJob.mockReturnValue(() => undefined)
})
afterEach(cleanup)

function artifact(kind: string, mediaType = 'image/png'): Artifact {
  return {
    id: `${kind}-id`,
    revision_id: 'revision-id',
    kind,
    filename: `${kind}.png`,
    media_type: mediaType,
    byte_size: 123,
    download_url: `/artifacts/${kind}`,
  }
}

describe('artifact to workspace layer integration', () => {
  it.each(['trace-fallbacks', 'trace-semantic-warnings'])(
    'maps the %s inspection raster to a toggleable overlay',
    kind => {
      expect(artifactToWorkspaceLayer(artifact(kind))?.role).toBe('overlay')
    },
  )

  it('preserves mask and comparison-image roles', () => {
    expect(artifactToWorkspaceLayer(artifact('edit-mask'))?.role).toBe('mask')
    for (const kind of ['source', 'flattened', 'report', 'traced-preview', 'inkscape-preview']) {
      expect(artifactToWorkspaceLayer(artifact(kind))?.role).toBe('image')
    }
    expect(artifactToWorkspaceLayer(artifact('traced-overlap', 'image/svg+xml'))?.role).toBe('image')
  })

  it('excludes non-visual audit and inspection artifacts', () => {
    expect(artifactToWorkspaceLayer(artifact('trace-audit', 'application/json'))).toBeNull()
    expect(artifactToWorkspaceLayer(artifact('trace-inspection', 'text/html'))).toBeNull()
  })
})

describe('application status accessibility', () => {
  it('announces actionable errors assertively', async () => {
    mocks.projects.mockRejectedValue(new Error('Project service unavailable'))
    render(createElement((await import('./App')).default))

    expect((await screen.findByRole('alert')).textContent).toContain('Project service unavailable')
  })

  it('exposes job progress without duplicating the polite status region', async () => {
    mocks.generate.mockResolvedValue({ project: { ...project, revisions: [revision] }, job: queuedJob, expanded_prompt: 'Expanded' })
    render(createElement((await import('./App')).default))
    fireEvent.change(screen.getByLabelText('Short subject'), { target: { value: 'sky whale' } })
    fireEvent.submit(screen.getByLabelText('Short subject').closest('form')!)

    const progress = await screen.findByRole('progressbar', { name: 'Job progress' })
    expect(progress.getAttribute('aria-valuemin')).toBe('0')
    expect(progress.getAttribute('aria-valuemax')).toBe('100')
    expect(progress.getAttribute('aria-valuenow')).toBe('25')
    expect(progress.getAttribute('aria-valuetext')).toBe('Preparing artwork · 25%')
    const statuses = screen.getAllByRole('status')
    expect(statuses).toHaveLength(1)
    expect(statuses[0].getAttribute('aria-live')).toBe('polite')
    expect(statuses[0].getAttribute('aria-atomic')).toBe('true')
    expect(statuses[0].textContent).toContain('Preparing artwork · 25%')
  })

  it('announces cancellation and completion through the same atomic status', async () => {
    let update: ((job: Job) => void) | undefined
    mocks.followJob.mockImplementation((_id, onJob) => { update = onJob; return () => undefined })
    mocks.generate.mockResolvedValue({ project: { ...project, revisions: [revision] }, job: queuedJob, expanded_prompt: 'Expanded' })
    render(createElement((await import('./App')).default))
    fireEvent.change(screen.getByLabelText('Short subject'), { target: { value: 'sky whale' } })
    fireEvent.submit(screen.getByLabelText('Short subject').closest('form')!)
    await screen.findByRole('progressbar')

    await act(async () => update?.({ ...queuedJob, status: 'cancelled', progress: 0.4, message: 'Generation cancelled' }))
    expect(screen.getByRole('status').textContent).toContain('Generation cancelled · 40%')
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('40')

    await act(async () => update?.({ ...queuedJob, status: 'succeeded', progress: 1, message: 'Artwork ready', result_revision_id: revision.id }))
    expect(screen.getByRole('status').textContent).toContain('Artwork ready · 100%')
  })

  it('announces failed jobs assertively with their error', async () => {
    let update: ((job: Job) => void) | undefined
    mocks.followJob.mockImplementation((_id, onJob) => { update = onJob; return () => undefined })
    mocks.generate.mockResolvedValue({ project: { ...project, revisions: [revision] }, job: queuedJob, expanded_prompt: 'Expanded' })
    render(createElement((await import('./App')).default))
    fireEvent.change(screen.getByLabelText('Short subject'), { target: { value: 'sky whale' } })
    fireEvent.submit(screen.getByLabelText('Short subject').closest('form')!)
    await screen.findByRole('progressbar')

    await act(async () => update?.({ ...queuedJob, status: 'failed', progress: 0.5, message: 'Generation stopped', error: 'Provider timed out' }))
    const alert = screen.getByRole('alert')
    expect(alert.getAttribute('aria-live')).toBe('assertive')
    expect(alert.textContent).toContain('Provider timed out · 50%')
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('announces successful Inkscape handoff actions politely', async () => {
    const svg = artifact('traced-overlap', 'image/svg+xml')
    mocks.projects.mockResolvedValue([project])
    mocks.effectiveArtifacts.mockResolvedValue([svg])
    mocks.inkscapeHandoff.mockResolvedValue({ exports: [svg], shared_folder_enabled: true, guidance: 'Inspect before cutting.' })
    mocks.saveForInkscape.mockResolvedValue({ filename: 'traced-overlap.svg', shared_path: '/shared/traced-overlap.svg' })
    render(createElement((await import('./App')).default))
    fireEvent.click(await screen.findByRole('button', { name: /Sky whale/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save SVG to shared folder' }))

    const status = await screen.findByRole('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toContain('Saved traced-overlap.svg to the shared folder.')
  })
})
