// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Job, Project, Revision } from './api'
import { ancestorPaletteIterations, ancestorTraceIterations, artifactToWorkspaceLayer, customRatioDimensions, inheritedPipelineSettings, stageFromHash } from './App'

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(),
  generate: vi.fn(), startPipeline: vi.fn(), inkscapeHandoff: vi.fn(), saveForInkscape: vi.fn(),
  followJob: vi.fn(),
}))

vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})
const workspaceProps = vi.hoisted(() => ({ current: null as { onSelectPoint?: (point: [number, number]) => void } | null }))
vi.mock('./ImageWorkspace', () => ({ ImageWorkspace: (props: { onSelectPoint?: (point: [number, number]) => void }) => { workspaceProps.current = props; return null } }))
vi.mock('./FlattenedEditor', () => ({ FlattenedEditor: () => null }))

const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }
const revision: Revision = { id: 'revision-1', project_id: project.id, parent_revision_id: null, kind: 'source', message: null, settings: {}, created_at: '2026-01-01' }
const queuedJob: Job = { id: 'job-1', project_id: project.id, revision_id: revision.id, result_revision_id: null, kind: 'generate', status: 'queued', progress: 0.25, message: 'Preparing artwork', error: null }

beforeEach(() => {
  vi.clearAllMocks()
  workspaceProps.current = null
  localStorage.clear()
  window.history.replaceState(null, '', '/')
  window.scrollTo = vi.fn()
  mocks.projects.mockResolvedValue([])
  mocks.project.mockResolvedValue({ ...project, revisions: [revision] })
  mocks.effectiveArtifacts.mockResolvedValue([])
  mocks.artifacts.mockResolvedValue([])
  mocks.editor.mockResolvedValue({ width: 0, height: 0, protected_pixels: 0, palette: [] })
  mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
  mocks.startPipeline.mockResolvedValue(queuedJob)
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

describe('generation frame dimensions', () => {
  it('uses the largest exact dimensions available for a custom ratio', () => {
    expect(customRatioDimensions(16, 9)).toEqual([1536, 864])
    expect(customRatioDimensions(4, 5)).toEqual([1024, 1280])
    expect(customRatioDimensions(1, 1)).toEqual([1024, 1024])
    expect(customRatioDimensions(8, 7)).toEqual([1024, 896])
    expect(customRatioDimensions(7, 8)).toEqual([896, 1024])
    expect(customRatioDimensions(23, 20)).toEqual([1012, 880])
    expect(customRatioDimensions(29, 25)).toEqual([1160, 1000])
    expect(customRatioDimensions(192, 108)).toEqual([1536, 864])
    expect(customRatioDimensions(7, 1)).toBeNull()
  })

  it('shows ratio inputs only for a custom frame and submits derived dimensions', async () => {
    mocks.generate.mockResolvedValue({ project: { ...project, revisions: [revision] }, job: queuedJob, expanded_prompt: 'Expanded' })
    render(createElement((await import('./App')).default))

    expect(screen.queryByLabelText('Ratio width')).toBeNull()
    fireEvent.change(screen.getByLabelText(/Frame/), { target: { value: 'custom' } })
    fireEvent.change(screen.getByLabelText('Ratio width'), { target: { value: '4' } })
    fireEvent.change(screen.getByLabelText('Ratio height'), { target: { value: '5' } })
    expect(screen.getByText('Maximum output: 1024 × 1280 px')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Short subject'), { target: { value: 'sky whale' } })
    const colourCount = screen.getByLabelText(/Number of colours/)
    expect((colourCount as HTMLInputElement).value).toBe('6')
    fireEvent.change(colourCount, { target: { value: '9' } })
    fireEvent.submit(screen.getByLabelText('Short subject').closest('form')!)

    await vi.waitFor(() => expect(mocks.generate).toHaveBeenCalled())
    expect(mocks.generate.mock.calls[0][2]).toMatchObject({ width: 1024, height: 1280 })
    expect(mocks.generate.mock.calls[0][2]).toMatchObject({ desired_colours: 9 })
    expect(screen.queryByLabelText('Art detail')).toBeNull()
    expect(screen.queryByLabelText('Quality')).toBeNull()
    expect(screen.getByLabelText(/Number of colours/).getAttribute('min')).toBe('2')
    expect(screen.getByLabelText(/Number of colours/).getAttribute('max')).toBe('12')
    expect(screen.queryByText('Expanded prompt sent to provider')).toBeNull()
  })

  it('associates invalid guidance with both ratio fields and blocks generation', async () => {
    render(createElement((await import('./App')).default))
    fireEvent.change(screen.getByLabelText(/Frame/), { target: { value: 'custom' } })
    fireEvent.change(screen.getByLabelText('Ratio width'), { target: { value: '7' } })
    fireEvent.change(screen.getByLabelText('Ratio height'), { target: { value: '1' } })

    const guidance = screen.getByRole('status')
    expect(guidance.textContent).toBe('Use a whole-number ratio between 1:6 and 6:1.')
    for (const name of ['Ratio width', 'Ratio height']) {
      const input = screen.getByLabelText(name)
      expect(input.getAttribute('aria-describedby')).toBe(guidance.id)
      expect(input.getAttribute('aria-invalid')).toBe('true')
    }
    fireEvent.change(screen.getByLabelText('Short subject'), { target: { value: 'sky whale' } })
    fireEvent.submit(screen.getByLabelText('Short subject').closest('form')!)

    expect((await screen.findByRole('alert')).textContent).toContain('Choose a whole-number ratio between 1:6 and 6:1')
    expect(mocks.generate).not.toHaveBeenCalled()
  })

  it('prefills the Create form from the source revision that produced the shown image', async () => {
    const generated = { ...revision, settings: { short_prompt: 'sky whale city', width: 1024, height: 1024, desired_colours: 8, required_motifs: ['whale'], rejected_motifs: ['text'] } }
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [generated] })
    mocks.effectiveArtifacts.mockResolvedValue([{ ...artifact('source'), revision_id: generated.id }])
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })

    await vi.waitFor(() => expect((screen.getByLabelText('Short subject') as HTMLTextAreaElement).value).toBe('sky whale city'))
    expect((screen.getByLabelText(/Number of colours/) as HTMLInputElement).value).toBe('8')
    expect((screen.getByLabelText(/Required motifs/) as HTMLInputElement).value).toBe('whale')
    expect((screen.getByLabelText(/Rejected motifs/) as HTMLInputElement).value).toBe('text')
    expect((screen.getByLabelText(/Frame/) as HTMLSelectElement).value).toBe('square')
  })
})

describe('pipeline setting provenance', () => {
  it('restores palette and trace settings independently through revision ancestry', () => {
    const palette = { ...revision, id: 'palette', kind: 'palette', settings: { palette: { initial_colours: 9 } } }
    const edit = { ...revision, id: 'edit', parent_revision_id: 'palette', kind: 'label-edit', settings: {} }
    const trace = { ...revision, id: 'trace', parent_revision_id: 'edit', kind: 'trace', settings: { trace: { curve_fit_tolerance: .4 } } }
    expect(inheritedPipelineSettings([trace, edit, palette], 'trace')).toEqual({
      palette: { initial_colours: 9 }, trace: { curve_fit_tolerance: .4 },
    })
  })

  it('lists each stage iteration along the ancestry, newest first and merged over defaults', () => {
    const older = { ...revision, id: 'p1', kind: 'palette', settings: { palette: { initial_colours: 8 } } }
    const edit = { ...revision, id: 'e1', parent_revision_id: 'p1', kind: 'label-edit', settings: {} }
    const newer = { ...revision, id: 'p2', parent_revision_id: 'e1', kind: 'palette', settings: { palette: { initial_colours: 20 } } }
    const traced = { ...revision, id: 't1', parent_revision_id: 'p2', kind: 'trace', settings: { trace: { overlap: 3 } } }
    const revisions = [traced, newer, edit, older]

    const palettes = ancestorPaletteIterations(revisions, 't1')
    expect(palettes.map(item => item.initial_colours)).toEqual([20, 8])
    // Fields the stored settings never recorded fall back to defaults, so every
    // iteration is a complete settings object the form can load.
    expect(palettes[0].protect_intentional_lines).toBe(true)
    expect(palettes[0].final_colours).toBeNull()

    const traces = ancestorTraceIterations(revisions, 't1')
    expect(traces.map(item => item.overlap)).toEqual([3])
    expect(traces[0].curve_optimization).toBe(true)
  })

  it('steps the flatten form through prior iterations and restores un-run edits, image unchanged', async () => {
    const older = { ...revision, id: 'palette-a', kind: 'palette', settings: { palette: { initial_colours: 8 } } }
    const newer = { ...revision, id: 'palette-b', parent_revision_id: 'palette-a', kind: 'palette', settings: { palette: { initial_colours: 20 } } }
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [newer, older] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened')])
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))

    // Both stage panels mount a stepper, so scope queries to the flatten panel.
    const panel = screen.getByRole('tabpanel', { name: 'Flatten' })
    const field = await within(panel).findByLabelText(/Initial candidates/) as HTMLInputElement
    await vi.waitFor(() => expect(field.value).toBe('20'))
    // Edit the working form, then scrub back to the older iteration.
    fireEvent.change(field, { target: { value: '13' } })
    fireEvent.click(within(panel).getByLabelText("Load an older iteration's settings"))
    expect(field.value).toBe('8')
    expect(within(panel).getByText('Iteration 1 of 2')).toBeTruthy()
    // Stepping forward returns to the working form with the un-run edit intact.
    fireEvent.click(within(panel).getByLabelText("Load a newer iteration's settings"))
    expect(field.value).toBe('13')
    expect(within(panel).getByText('Iteration 2 of 2')).toBeTruthy()
  })

  it('activates the shared canvas while a preservation point is being selected', async () => {
    mocks.projects.mockResolvedValue([project])
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source')])
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    fireEvent.click(screen.getByRole('button', { name: /Protect connected detail/ }))
    expect(document.querySelector('.artwork-surface')?.className).toContain('mobile-stage-active')
  })

  it('routes edit-stage canvas clicks to area selection, never preservation, after leaving flatten', async () => {
    mocks.projects.mockResolvedValue([project])
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened')])
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    fireEvent.click(screen.getByRole('button', { name: /Protect connected detail/ }))
    expect(screen.getByText('Select the intended source detail.')).toBeTruthy()

    // Leaving flatten clears the stale preservation mode; on Edit the canvas is a
    // live area selector, so a click must not add a preservation point.
    fireEvent.click(screen.getByRole('tab', { name: 'Edit' }))
    await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeInstanceOf(Function))
    act(() => workspaceProps.current?.onSelectPoint?.([12, 34]))

    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    expect(screen.queryByText('Protected details')).toBeNull()
  })

  it('flags an edited field with a pending dot and saves the draft on blur, not on keystroke', async () => {
    mocks.projects.mockResolvedValue([project])
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    const field = screen.getByLabelText(/Initial candidates/)

    fireEvent.change(field, { target: { value: '23' } })
    // The dot tracks divergence from the baseline immediately; the draft is only
    // committed to storage once the field loses focus.
    expect(field.closest('label')?.querySelector('.field-dirty')).toBeTruthy()
    expect(localStorage.getItem(`vinylgen.project-settings.${project.id}`)).toBeNull()

    fireEvent.blur(field)
    expect(JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) || '{}').palette.initial_colours).toBe(23)
  })

  it('fills flatten fields from the producing revision and shows no pending dot', async () => {
    const flattened = { ...revision, id: 'palette-1', kind: 'palette', settings: { palette: { initial_colours: 30 } } }
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [flattened] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened')])
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))

    const field = await screen.findByLabelText(/Initial candidates/)
    await vi.waitFor(() => expect((field as HTMLInputElement).value).toBe('30'))
    expect(field.closest('label')?.querySelector('.field-dirty')).toBeNull()
  })

  it('clears the step draft once the stage is run', async () => {
    mocks.projects.mockResolvedValue([project])
    localStorage.setItem(`vinylgen.project-settings.${project.id}`,
      JSON.stringify({ palette: { initial_colours: 23 }, trace: { overlap: 4 } }))
    mocks.followJob.mockImplementation((_id: string, onJob: (job: Job) => void) => {
      onJob({ ...queuedJob, kind: 'palette', status: 'succeeded', result_revision_id: 'palette-2' })
      return () => undefined
    })
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    const run = screen.getByRole('button', { name: 'Flatten image' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)

    // The palette run bakes the values into a revision; its draft is dropped while
    // the untouched trace draft is left intact.
    await vi.waitFor(() => {
      const stored = JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) || '{}')
      expect(stored.palette).toBeUndefined()
      expect(stored.trace).toEqual({ overlap: 4 })
    })
  })
})

describe('workflow tab navigation', () => {
  it('uses a stable default and accepts only known URL stages', () => {
    expect(stageFromHash('')).toBe('create')
    expect(stageFromHash('#stage=edit')).toBe('edit')
    expect(stageFromHash('#stage=unknown')).toBe('create')
  })

  it('restores the URL stage and current work on reload with current-step semantics', async () => {
    mocks.projects.mockResolvedValue([project])
    window.history.replaceState(null, '', `/#stage=export&project=${project.id}&revision=${revision.id}`)
    render(createElement((await import('./App')).default))

    expect(screen.getByRole('tab', { name: 'Export' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('tabpanel', { name: 'Export' }).className).toContain('mobile-stage-active')
    expect((await screen.findByLabelText('Current work')).textContent).toContain('Sky whale')
    expect(screen.getByLabelText('Current work').textContent).toContain('Export')
  })

  it('writes stage changes and restores focus through real back and forward navigation', async () => {
    render(createElement((await import('./App')).default))
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    await vi.waitFor(() => expect(window.location.hash).toBe('#stage=flatten'))
    expect(screen.getByRole('tab', { name: 'Flatten' }).getAttribute('aria-selected')).toBe('true')
    fireEvent.click(screen.getByRole('tab', { name: 'Edit' }))
    await vi.waitFor(() => expect(window.location.hash).toBe('#stage=edit'))

    window.history.back()
    await vi.waitFor(() => expect(screen.getByRole('tab', { name: 'Flatten' }).getAttribute('aria-selected')).toBe('true'))
    await vi.waitFor(() => expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Flatten' })))
    window.history.forward()
    await vi.waitFor(() => expect(screen.getByRole('tab', { name: 'Edit' }).getAttribute('aria-selected')).toBe('true'))
    await vi.waitFor(() => expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Edit' })))
  })

  it('supports roving focus and arrow-key tab selection', async () => {
    render(createElement((await import('./App')).default))
    const createTab = screen.getByRole('tab', { name: 'Create' })
    createTab.focus()
    fireEvent.keyDown(createTab, { key: 'ArrowRight' })

    await vi.waitFor(() => expect(screen.getByRole('tab', { name: 'Upload' }).getAttribute('aria-selected')).toBe('true'))
    await vi.waitFor(() => expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Upload' })))
    expect(screen.getByRole('tab', { name: 'Create' }).tabIndex).toBe(-1)
    expect(screen.getByRole('tabpanel', { name: 'Upload' })).toBeTruthy()
  })

  it('exposes the desktop rail as vertical and supports vertical arrow keys', async () => {
    render(createElement((await import('./App')).default))
    const tablist = screen.getByRole('tablist', { name: 'Artwork workflow' })
    expect(tablist.getAttribute('aria-orientation')).toBe('vertical')

    const createTab = screen.getByRole('tab', { name: 'Create' })
    createTab.focus()
    fireEvent.keyDown(createTab, { key: 'ArrowDown' })

    await vi.waitFor(() => expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Upload' })))
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
    expect(progress.getAttribute('aria-valuenow')).toBeNull()
    const statuses = screen.getAllByRole('status')
    expect(statuses).toHaveLength(1)
    expect(statuses[0].getAttribute('aria-live')).toBe('polite')
    expect(statuses[0].getAttribute('aria-atomic')).toBe('true')
    expect(statuses[0].textContent).toContain('Preparing artwork')
  })

  it('fills the progress bar and shows the current stage once a running job reports progress', async () => {
    let update: ((job: Job) => void) | undefined
    mocks.followJob.mockImplementation((_id, onJob) => { update = onJob; return () => undefined })
    mocks.generate.mockResolvedValue({ project: { ...project, revisions: [revision] }, job: queuedJob, expanded_prompt: 'Expanded' })
    render(createElement((await import('./App')).default))
    fireEvent.change(screen.getByLabelText('Short subject'), { target: { value: 'sky whale' } })
    fireEvent.submit(screen.getByLabelText('Short subject').closest('form')!)
    const progress = await screen.findByRole('progressbar', { name: 'Job progress' })
    // Queued: no measured value yet, so the bar stays indeterminate and the spinner shows.
    expect(progress.getAttribute('aria-valuenow')).toBeNull()
    expect(document.querySelector('.spinner')).not.toBeNull()
    expect(screen.queryByText('40%')).toBeNull()

    await act(async () => update?.({ ...queuedJob, status: 'running', progress: 0.4, message: 'Resolving anti-aliased boundaries' }))
    expect(progress.getAttribute('aria-valuenow')).toBe('40')
    expect(progress.getAttribute('style')).toContain('--progress: 0.4')
    expect(screen.getByRole('status').textContent).toContain('Resolving anti-aliased boundaries')
    // Determinate: a visible percent readout appears and the redundant spinner is dropped.
    expect(screen.getByText('40%')).not.toBeNull()
    expect(document.querySelector('.spinner')).toBeNull()
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
    expect(screen.getByRole('status').textContent).toContain('Generation cancelled')
    expect(screen.queryByRole('progressbar')).toBeNull()

    await act(async () => update?.({ ...queuedJob, status: 'succeeded', progress: 1, message: 'Artwork ready', result_revision_id: revision.id }))
    expect(screen.getByRole('status').textContent).toContain('Artwork ready')
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
    expect(alert.textContent).toContain('Provider timed out')
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('announces successful Inkscape handoff actions politely', async () => {
    const svg = artifact('traced-overlap', 'image/svg+xml')
    mocks.projects.mockResolvedValue([project])
    mocks.effectiveArtifacts.mockResolvedValue([svg])
    mocks.inkscapeHandoff.mockResolvedValue({ exports: [svg], shared_folder_enabled: true, guidance: 'Inspect before cutting.' })
    mocks.saveForInkscape.mockResolvedValue({ filename: 'traced-overlap.svg', shared_path: '/shared/traced-overlap.svg' })
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })
    fireEvent.click(await screen.findByRole('button', { name: 'Save SVG to shared folder' }))

    const status = await screen.findByRole('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toContain('Saved traced-overlap.svg to the shared folder.')
  })
})

describe('artwork view toggle', () => {
  const paletteRevision: Revision = { ...revision, id: 'palette-1', kind: 'palette' }
  const editRevision: Revision = { ...revision, id: 'edit-1', parent_revision_id: 'palette-1', kind: 'label-edit' }

  it('offers only the views the revision lineage has reached', async () => {
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [paletteRevision] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened')])
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })

    const group = await screen.findByRole('group', { name: 'Artwork view' })
    expect(within(group).getByRole('button', { name: 'Source' })).toBeTruthy()
    expect(within(group).getByRole('button', { name: 'Flattened' })).toBeTruthy()
    expect(within(group).queryByRole('button', { name: 'Edited' })).toBeNull()
    expect(within(group).queryByRole('button', { name: 'Traced' })).toBeNull()
  })

  it('drives the canvas image and disables area editing off the editable image', async () => {
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [paletteRevision] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened')])
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })
    const group = await screen.findByRole('group', { name: 'Artwork view' })

    fireEvent.click(screen.getByRole('tab', { name: 'Edit' }))
    await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeInstanceOf(Function))

    fireEvent.click(within(group).getByRole('button', { name: 'Source' }))
    await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeUndefined())
    expect(screen.getByText(/Switch to the Flattened image to select and edit areas\./)).toBeTruthy()
  })

  it('adds an Edited view for edited revisions and restricts editing to it', async () => {
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [editRevision, paletteRevision] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened')])
    mocks.artifacts.mockResolvedValue([{ ...artifact('flattened'), id: 'flattened-original', revision_id: 'palette-1' }])
    render(createElement((await import('./App')).default))
    fireEvent.change(await screen.findByLabelText('Active project'), { target: { value: project.id } })
    const group = await screen.findByRole('group', { name: 'Artwork view' })
    expect(within(group).getByRole('button', { name: 'Edited' })).toBeTruthy()
    expect(within(group).getByRole('button', { name: 'Flattened' })).toBeTruthy()

    fireEvent.click(screen.getByRole('tab', { name: 'Edit' }))
    await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeInstanceOf(Function))

    fireEvent.click(within(group).getByRole('button', { name: 'Flattened' }))
    await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeUndefined())
    expect(screen.getByText(/Switch to the Edited image to select and edit areas\./)).toBeTruthy()
  })
})
