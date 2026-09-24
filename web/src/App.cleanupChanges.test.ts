// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Project, Revision } from './api'
import { DEFAULT_CLEANUP_SETTINGS } from './PipelineControls'

// The real canvas, so the Show changes switch App configures is the one rendered.
const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(),
  inkscapeHandoff: vi.fn(), health: vi.fn(), followJob: vi.fn(),
}))
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})

const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }
const flattenRevision: Revision = { id: 'palette-1', project_id: project.id, parent_revision_id: null, kind: 'palette', message: null, settings: { palette: {} }, created_at: '2026-01-01' }
const cleanupRevision: Revision = { id: 'cleanup-1', project_id: project.id, parent_revision_id: flattenRevision.id, kind: 'cleanup', message: null, settings: { cleanup: { ...DEFAULT_CLEANUP_SETTINGS, cleanup: true, min_region_area: 4 } }, created_at: '2026-01-02' }

function artifact(kind: string, revision: Revision = flattenRevision): Artifact {
  return { id: `${kind}-${revision.id}`, revision_id: revision.id, kind, filename: `${kind}.png`, media_type: 'image/png', byte_size: 1, download_url: `/artifacts/${kind}-${revision.id}` }
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  window.history.replaceState(null, '', '/')
  window.scrollTo = vi.fn()
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  mocks.projects.mockResolvedValue([project])
  mocks.project.mockResolvedValue({ ...project, revisions: [cleanupRevision, flattenRevision] })
  mocks.artifacts.mockResolvedValue([])
  mocks.editor.mockResolvedValue({ width: 1200, height: 900, protected_pixels: 0, palette: [] })
  mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
  mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
  mocks.followJob.mockReturnValue(() => undefined)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

async function openCleanup() {
  render(createElement((await import('./App')).default))
  await screen.findByRole('option', { name: project.name })
  fireEvent.change(screen.getByLabelText('Active project'), { target: { value: project.id } })
  fireEvent.click(screen.getByRole('tab', { name: 'Cleanup' }))
  return await screen.findByRole('switch', { name: 'Show changes' }) as HTMLInputElement
}

describe('Cleanup canvas Show changes switch', () => {
  it('is disabled, saying why, when the cleanup recorded no changes overlay', async () => {
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened', cleanupRevision)])
    const toggle = await openCleanup()
    await vi.waitFor(() => expect(document.getElementById(toggle.getAttribute('aria-describedby') ?? '')?.textContent).toBe('Re-run Cleanup to record its changes'))
    expect(toggle.disabled).toBe(true)
    expect(toggle.checked).toBe(false)
  })

  it('is enabled and on when the cleanup recorded one', async () => {
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened', cleanupRevision), artifact('cleanup-changes-overlay', cleanupRevision)])
    const toggle = await openCleanup()
    await vi.waitFor(() => expect(toggle.disabled).toBe(false))
    expect(toggle.checked).toBe(true)
  })
})
