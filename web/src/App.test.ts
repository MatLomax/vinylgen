// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Job, Project, Revision } from './api'
import { DEFAULT_TRACE_SETTINGS } from './PipelineControls'
import { activeCleanupRevision, ancestorCleanupIterations, ancestorPaletteIterations, artifactToWorkspaceLayer, canvasOverlayKinds, customRatioDimensions, inheritedPipelineSettings, stageFromHash, traceRunChain, traceRunIsPhysical, tracedSvgKind, tracedSvgName } from './App'

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(), automaticLayerOrder: vi.fn(),
  generate: vi.fn(), startPipeline: vi.fn(), inkscapeHandoff: vi.fn(), saveForInkscape: vi.fn(),
  importProject: vi.fn(), health: vi.fn(), deleteProject: vi.fn(),
  followJob: vi.fn(),
}))

vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})
type CapturedWorkspace = { onSelectPoint?: (point: [number, number]) => void; preferredLayerId?: string; editorMode?: boolean; canvasLabel?: string; loading?: boolean; layers?: { id: string; name: string; role?: string; sizeFrom?: string }[]; inspectionKinds?: readonly string[]; markers?: { id: string; kind: string; label: string; points: [number, number][]; pending?: boolean }[]; markerGridFrom?: string; highlightedMarker?: string | null; pickAim?: boolean }
const workspaceProps = vi.hoisted(() => ({ current: null as CapturedWorkspace | null }))
vi.mock('./ImageWorkspace', () => ({ ImageWorkspace: (props: CapturedWorkspace) => { workspaceProps.current = props; return null } }))
const editorProps = vi.hoisted(() => ({ current: null as { onCommit: (id: string) => void } | null }))
vi.mock('./FlattenedEditor', () => ({ FlattenedEditor: (props: { onCommit: (id: string) => void }) => { editorProps.current = props; return null } }))

const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }
const revision: Revision = { id: 'revision-1', project_id: project.id, parent_revision_id: null, kind: 'source', message: null, settings: {}, created_at: '2026-01-01' }
const queuedJob: Job = { id: 'job-1', project_id: project.id, revision_id: revision.id, result_revision_id: null, kind: 'generate', status: 'queued', progress: 0.25, message: 'Preparing artwork', error: null }

beforeEach(() => {
  vi.clearAllMocks()
  workspaceProps.current = null
  editorProps.current = null
  localStorage.clear()
  window.history.replaceState(null, '', '/')
  window.scrollTo = vi.fn()
  mocks.projects.mockResolvedValue([])
  mocks.project.mockResolvedValue({ ...project, revisions: [revision] })
  mocks.effectiveArtifacts.mockResolvedValue([])
  mocks.artifacts.mockResolvedValue([])
  mocks.editor.mockResolvedValue({ width: 0, height: 0, protected_pixels: 0, palette: [] })
  mocks.automaticLayerOrder.mockResolvedValue({ automatic_layer_order: null })
  mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
  mocks.startPipeline.mockResolvedValue(queuedJob)
  mocks.importProject.mockResolvedValue({ ...project, revisions: [revision] })
  mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
  mocks.followJob.mockReturnValue(() => undefined)
  mocks.deleteProject.mockResolvedValue(undefined)
})
afterEach(cleanup)

// Pick a project from the header only once its option has rendered: the select is on the
// page before the project list loads, and choosing a value it lacks selects nothing.
async function chooseProject(target: Project = project) {
  await screen.findByRole('option', { name: target.name })
  fireEvent.change(screen.getByLabelText('Active project'), { target: { value: target.id } })
}

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
  it.each([
    ['trace-missed-details-overlay', 'Missed source details (red)'],
    ['trace-fallbacks-overlay', 'Tightened boundaries'],
    ['protected-areas-overlay', 'Protected areas'],
  ])('maps the transparent %s to a labelled toggleable overlay', (kind, label) => {
    expect(artifactToWorkspaceLayer(artifact(kind))).toMatchObject({ role: 'overlay', name: label })
  })

  it.each(['trace-fallbacks', 'trace-semantic-warnings'])(
    'never offers the opaque %s audit image as a layer',
    kind => {
      expect(artifactToWorkspaceLayer(artifact(kind))).toBeNull()
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

describe('trace run chain', () => {
  const at = (minute: number) => `2026-01-01T00:${String(minute).padStart(2, '0')}:00Z`
  const rev = (id: string, parent: string | null, kind: string, minute: number): Revision =>
    ({ ...revision, id, parent_revision_id: parent, kind, created_at: at(minute) })
  // An older run before an edit, then three runs on the edited artwork with a validate
  // record between two of them and an abandoned branch off the first.
  const revisions = [
    rev('p', null, 'palette', 0), rev('old', 'p', 'trace', 1), rev('e', 'old', 'label-edit', 2),
    rev('t1', 'e', 'trace', 3), rev('t2', 't1', 'trace', 4), rev('v2', 't2', 'validate', 5),
    rev('t3', 'v2', 'trace', 6), rev('branch', 't1', 'trace', 7),
  ]

  it('lists the runs on the shown artwork newest first without crossing an edit', () => {
    expect(traceRunChain(revisions, 't2')).toEqual({ runs: [revisions[6], revisions[4], revisions[3]], index: 1 })
  })

  it('follows the newest trace child forward, as Redo does', () => {
    // t1's newest child is the later branch, so stepping forward from t1 goes there.
    const chain = traceRunChain(revisions, 't1')
    expect(chain.runs.map(row => row.id)).toEqual(['branch', 't1'])
    expect(chain.index).toBe(1)
  })

  it('does not let a trailing export on an older run hide the newer runs', () => {
    const exported = [...revisions, rev('x', 't2', 'export', 8)]
    expect(traceRunChain(exported, 'x').runs.map(row => row.id)).toEqual(['t3', 't2', 't1'])
    expect(traceRunChain(exported, 't2').index).toBe(1)
  })

  it('places a validate record at the run it validated and is empty off a run', () => {
    expect(traceRunChain(revisions, 'v2').index).toBe(1)
    expect(traceRunChain(revisions, 'e')).toEqual({ runs: [], index: -1 })
    expect(traceRunChain(revisions, null)).toEqual({ runs: [], index: -1 })
  })
})

describe('traced SVG variant', () => {
  it('shows the overlap file only when the run asked for overlap or solid underlayers', () => {
    expect(tracedSvgKind(undefined)).toBe('traced-edge')
    expect(tracedSvgKind({ overlap: 0, solid_underlayers: false })).toBe('traced-edge')
    expect(tracedSvgKind({ overlap: 1.5, solid_underlayers: false })).toBe('traced-overlap')
    expect(tracedSvgKind({ overlap: 0, solid_underlayers: true })).toBe('traced-overlap')
  })

  it('names the variant with the overlap in the units the run used', () => {
    expect(tracedSvgName({ overlap: 0, solid_underlayers: false })).toBe('Traced SVG · edge to edge')
    expect(tracedSvgName({ overlap: 0, solid_underlayers: true })).toBe('Traced SVG · solid underlayers')
    expect(tracedSvgName({ overlap: 2, solid_underlayers: false }, true)).toBe('Traced SVG · 2 mm overlap')
    expect(tracedSvgName({ overlap: 2, solid_underlayers: false }, false)).toBe('Traced SVG · 2 px overlap')
  })

  it('takes a run\'s units from its cleanup, or from its own settings when recorded before the cleanup stage', () => {
    const palette: Revision = { ...revision, id: 'palette', kind: 'palette', settings: { stale_concerns: ['cleanup', 'trace', 'validation', 'export'] } }
    const cleaned: Revision = { ...revision, id: 'cleanup', parent_revision_id: 'palette', kind: 'cleanup', settings: { cleanup: { width_mm: 300, height_mm: null } } }
    const run: Revision = { ...revision, id: 'run', parent_revision_id: 'cleanup', kind: 'trace', settings: { trace: { overlap: 2 } } }
    const pixelRun: Revision = { ...run, id: 'pixel-run', parent_revision_id: 'palette' }
    const legacyRun: Revision = { ...run, id: 'legacy-run', parent_revision_id: 'palette', settings: { trace: { overlap: 2, width_mm: null, height_mm: 90 } } }
    const revisions = [run, pixelRun, legacyRun, cleaned, palette]
    expect(traceRunIsPhysical(revisions, 'run')).toBe(true)
    expect(traceRunIsPhysical(revisions, 'pixel-run')).toBe(false)
    expect(traceRunIsPhysical(revisions, 'legacy-run')).toBe(true)
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
    await chooseProject()

    await vi.waitFor(() => expect((screen.getByLabelText('Short subject') as HTMLTextAreaElement).value).toBe('sky whale city'))
    expect((screen.getByLabelText(/Number of colours/) as HTMLInputElement).value).toBe('8')
    expect((screen.getByLabelText(/Required motifs/) as HTMLInputElement).value).toBe('whale')
    expect((screen.getByLabelText(/Rejected motifs/) as HTMLInputElement).value).toBe('text')
    // The frame follows the generation in an effect, a render after the remounted form fields.
    await vi.waitFor(() => expect((screen.getByLabelText(/Frame/) as HTMLSelectElement).value).toBe('square'))
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

  it('restores cleanup settings from the nearest cleanup, and from a pre-cleanup-stage trace run', () => {
    const palette = { ...revision, id: 'palette', kind: 'palette', settings: { palette: { initial_colours: 9 } } }
    const legacy = { ...revision, id: 'legacy', parent_revision_id: 'palette', kind: 'trace', settings: { trace: { overlap: 1, width_mm: 400, height_mm: null, remove_colour: 'Sand', cleanup: true, cleanup_action: 'widen', min_detail_width: .5, min_region_area: 0, remove_below_width: 0 } } }
    const cleaned = { ...revision, id: 'cleanup', parent_revision_id: 'legacy', kind: 'cleanup', settings: { cleanup: { width_mm: 250, cleanup: false } } }
    const traced = { ...revision, id: 'trace', parent_revision_id: 'cleanup', kind: 'trace', settings: { trace: { overlap: 3 } } }
    const revisions = [traced, cleaned, legacy, palette]
    // The legacy run's embedded values fill the Cleanup form, never the Trace form.
    expect(inheritedPipelineSettings(revisions, 'legacy')).toEqual({
      palette: { initial_colours: 9 }, trace: { overlap: 1 },
      cleanup: { width_mm: 400, height_mm: null, remove_colour: 'Sand', cleanup: true, cleanup_action: 'widen', min_detail_width: .5, min_region_area: 0, remove_below_width: 0 },
      recordedRemoveColour: 'Sand',
    })
    expect(inheritedPipelineSettings(revisions, 'trace')).toEqual({
      palette: { initial_colours: 9 }, trace: { overlap: 3 }, cleanup: { width_mm: 250, cleanup: false },
    })
    // Only the real cleanup run is a step of the Cleanup form; the legacy trace run's
    // embedded values are its baseline (asserted above), never a countable iteration.
    expect(ancestorCleanupIterations(revisions, 'trace').map(item => [item.width_mm, item.cleanup])).toEqual([[250, false]])
  })

  it('counts only real cleanup revisions as Cleanup iterations, never a pre-cleanup-stage trace run', () => {
    const palette = { ...revision, id: 'palette', kind: 'palette', settings: { palette: {} } }
    const legacy = { ...revision, id: 'legacy', parent_revision_id: 'palette', kind: 'trace', settings: { trace: { overlap: 1, width_mm: 400, cleanup: true } } }
    expect(ancestorCleanupIterations([legacy, palette], 'legacy')).toEqual([])
  })

  it('counts each real cleanup revision along the ancestry as a Cleanup iteration, newest first', () => {
    const palette = { ...revision, id: 'palette', kind: 'palette', settings: { palette: {} } }
    const older = { ...revision, id: 'c1', parent_revision_id: 'palette', kind: 'cleanup', settings: { cleanup: { width_mm: 200 } } }
    const newer = { ...revision, id: 'c2', parent_revision_id: 'c1', kind: 'cleanup', settings: { cleanup: { width_mm: 300 } } }
    expect(ancestorCleanupIterations([newer, older, palette], 'c2').map(item => item.width_mm)).toEqual([300, 200])
  })

  it('finds the active cleanup until a flatten or edit marks it stale', () => {
    const palette = { ...revision, id: 'palette', kind: 'palette', settings: { stale_concerns: ['cleanup', 'trace', 'validation', 'export'] } }
    const cleaned = { ...revision, id: 'cleanup', parent_revision_id: 'palette', kind: 'cleanup', settings: { cleanup: { width_mm: 250 }, stale_concerns: ['trace', 'validation', 'export'] } }
    const traced = { ...revision, id: 'trace', parent_revision_id: 'cleanup', kind: 'trace', settings: { trace: {}, stale_concerns: ['export'] } }
    const edited = { ...revision, id: 'edit', parent_revision_id: 'trace', kind: 'label-edit', settings: { stale_concerns: ['cleanup', 'trace', 'validation', 'export'] } }
    const revisions = [edited, traced, cleaned, palette]
    expect(activeCleanupRevision(revisions, 'trace')?.id).toBe('cleanup')
    expect(activeCleanupRevision(revisions, 'cleanup')?.id).toBe('cleanup')
    expect(activeCleanupRevision(revisions, 'edit')).toBeNull()
    expect(activeCleanupRevision(revisions, 'palette')).toBeNull()
  })

  it('lists each flatten iteration along the ancestry, newest first and merged over defaults', () => {
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
  })

  it('maps inherited preservation points onto the grid of an upscaled source', () => {
    const picked = { protected_component_points: [[10, 20]], preserved_colour_points: [[63, 47]], distinct_colour_point_pairs: [[[0, 0], [31, 23]]] }
    const older = { ...revision, id: 'p1', kind: 'palette', settings: { palette: { ...picked, initial_colours: 8 } } }
    const upscaled = { ...revision, id: 'u1', parent_revision_id: 'p1', kind: 'upscaled-source', settings: { parent_source_size: [64, 48], source_size: [136, 104], stale_concerns: ['palette', 'cleanup', 'trace', 'validation', 'export'] } }
    const newer = { ...revision, id: 'p2', parent_revision_id: 'u1', kind: 'palette', settings: { palette: { initial_colours: 20, protected_component_points: [[100, 90]] } } }
    const revisions = [newer, upscaled, older]
    // On the upscaled source, the flatten picked on its parent lands on the same features:
    // floor((x + .5) * 136 / 64), floor((y + .5) * 104 / 48).
    const mapped = { protected_component_points: [[22, 44]], preserved_colour_points: [[134, 102]], distinct_colour_point_pairs: [[[1, 1], [66, 50]]] }
    expect(inheritedPipelineSettings(revisions, 'u1').palette).toEqual({ ...mapped, initial_colours: 8 })
    // The parent's own form keeps the points it was picked with.
    expect(inheritedPipelineSettings(revisions, 'p1').palette).toEqual({ ...picked, initial_colours: 8 })
    // Stepping back from a flatten of the upscaled source reaches the older run mapped
    // onto the grid it shows; the newer run was picked there and is unchanged.
    const iterations = ancestorPaletteIterations(revisions, 'p2')
    expect(iterations.map(item => item.protected_component_points)).toEqual([[[100, 90]], mapped.protected_component_points])
    expect(iterations[1].distinct_colour_point_pairs).toEqual(mapped.distinct_colour_point_pairs)
  })

  it('steps the flatten form through prior iterations and restores un-run edits, image unchanged', async () => {
    const older = { ...revision, id: 'palette-a', kind: 'palette', settings: { palette: { initial_colours: 8 } } }
    const newer = { ...revision, id: 'palette-b', parent_revision_id: 'palette-a', kind: 'palette', settings: { palette: { initial_colours: 20 } } }
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [newer, older] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened')])
    render(createElement((await import('./App')).default))
    await chooseProject()
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

  it('lets a focused number field be cleared to empty and reverts a required field left empty on blur', async () => {
    const older = { ...revision, id: 'palette-a', kind: 'palette', settings: { palette: { initial_colours: 20 } } }
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [older] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened')])
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))

    const panel = screen.getByRole('tabpanel', { name: 'Flatten' })
    const field = await within(panel).findByLabelText(/Initial candidates/) as HTMLInputElement
    await vi.waitFor(() => expect(field.value).toBe('20'))
    // A focused field accepts an empty value, so every digit can be backspaced.
    fireEvent.focus(field)
    fireEvent.change(field, { target: { value: '' } })
    expect(field.value).toBe('')
    fireEvent.change(field, { target: { value: '7' } })
    expect(field.value).toBe('7')
    fireEvent.blur(field)
    expect(field.value).toBe('7')
    // A required field left empty snaps back to its committed value on blur.
    fireEvent.focus(field)
    fireEvent.change(field, { target: { value: '' } })
    expect(field.value).toBe('')
    fireEvent.blur(field)
    expect(field.value).toBe('7')
  })

  it('activates the shared canvas while a preservation point is being selected', async () => {
    mocks.projects.mockResolvedValue([project])
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source')])
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    fireEvent.click(screen.getByRole('button', { name: /Protect connected detail/ }))
    expect(document.querySelector('.artwork-surface')?.className).toContain('mobile-stage-active')
  })

  it('routes edit-stage canvas clicks to area selection, never preservation, after leaving flatten', async () => {
    mocks.projects.mockResolvedValue([project])
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened')])
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    fireEvent.click(screen.getByRole('button', { name: /Protect connected detail/ }))
    expect(screen.getByText('Select the intended source detail.')).toBeTruthy()

    // Leaving flatten clears the stale preservation mode; on Edit the canvas is a
    // live area selector, so a click must not add a preservation point.
    fireEvent.click(screen.getByRole('tab', { name: 'Edit' }))
    await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeInstanceOf(Function))
    // The area selector picks without the preservation crosshair.
    expect(workspaceProps.current?.pickAim).toBe(false)
    act(() => workspaceProps.current?.onSelectPoint?.([12, 34]))

    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    expect(screen.queryByText('Protected details')).toBeNull()
  })

  it('flags an edited field with a pending dot and saves the draft on blur, not on keystroke', async () => {
    mocks.projects.mockResolvedValue([project])
    render(createElement((await import('./App')).default))
    await chooseProject()
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
    await chooseProject()
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
    await chooseProject()
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

  it('carries a draft picked before a source upscale onto the upscaled grid, in the markers and the run', async () => {
    const flattened = { ...revision, id: 'palette-1', kind: 'palette', settings: { palette: { protected_component_points: [[10, 20]] } } }
    const upscaled = { ...revision, id: 'upscaled-1', parent_revision_id: 'palette-1', kind: 'upscaled-source', created_at: '2026-01-02', settings: { parent_source_size: [64, 48], source_size: [136, 104], stale_concerns: ['palette', 'cleanup', 'trace', 'validation', 'export'] } }
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [upscaled, flattened] })
    // The un-run draft was picked on the flattened revision's (pre-upscale) source.
    localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({
      palette: { protected_component_points: [[10, 20]], preserved_colour_points: [[63, 47]], distinct_colour_point_pairs: [[[0, 0], [31, 23]]] },
      palette_revision: 'palette-1' }))
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))

    await vi.waitFor(() => expect(workspaceProps.current?.markers?.map(marker => [marker.kind, marker.points])).toEqual([
      ['protect', [[22, 44]]], ['preserve', [[134, 102]]], ['pair', [[1, 1], [66, 50]]]]))
    const run = screen.getByRole('button', { name: 'Flatten image' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalled())
    const [, , startedFrom, settings] = mocks.startPipeline.mock.calls[0]
    expect(startedFrom).toBe('upscaled-1')
    expect(settings.palette).toMatchObject({
      protected_component_points: [[22, 44]], preserved_colour_points: [[134, 102]], distinct_colour_point_pairs: [[[1, 1], [66, 50]]] })
  })

  // A flattened revision and the upscale made from it; the draft a test leaves is read back.
  const upscaleLineage = () => {
    const flattened = { ...revision, id: 'palette-1', kind: 'palette', settings: { palette: {} } }
    const upscaled = { ...revision, id: 'upscaled-1', parent_revision_id: 'palette-1', kind: 'upscaled-source', created_at: '2026-01-02', settings: { parent_source_size: [64, 48], source_size: [136, 104], stale_concerns: ['palette', 'cleanup', 'trace', 'validation', 'export'] } }
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [upscaled, flattened] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source')])
  }
  const storedPaletteDraft = () => JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) || '{}')
  const showRevision = (id: string) => act(() => {
    window.history.replaceState(null, '', `/#stage=flatten&project=${project.id}&revision=${id}`)
    window.dispatchEvent(new HashChangeEvent('hashchange'))
  })
  async function expectUpscaledRunMapped() {
    await vi.waitFor(() => expect(workspaceProps.current?.markers?.map(marker => [marker.kind, marker.points])).toEqual([['protect', [[22, 44]]]]))
    const run = screen.getByRole('button', { name: 'Flatten image' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalled())
    const [, , startedFrom, settings] = mocks.startPipeline.mock.calls[0]
    expect(startedFrom).toBe('upscaled-1')
    expect(settings.palette.protected_component_points).toEqual([[22, 44]])
  }

  it('maps a point picked on the parent, and saved as the draft, onto the upscaled child', async () => {
    upscaleLineage()
    window.history.replaceState(null, '', `/#stage=flatten&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    const panel = await screen.findByRole('tabpanel', { name: 'Flatten' })
    fireEvent.click(await within(panel).findByRole('button', { name: /Protect connected detail/ }))
    await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeInstanceOf(Function))
    act(() => workspaceProps.current?.onSelectPoint?.([10, 20]))
    // Leaving the Flatten settings saves the draft, named for the revision it was picked on.
    fireEvent.blur(within(panel).getByLabelText(/Initial candidates/))
    expect(storedPaletteDraft().palette.protected_component_points).toEqual([[10, 20]])
    expect(storedPaletteDraft().palette_revision).toBe('palette-1')
    await vi.waitFor(() => expect(workspaceProps.current?.markers?.map(marker => marker.points)).toEqual([[[10, 20]]]))

    showRevision('upscaled-1')
    await expectUpscaledRunMapped()
  })

  it('names the shown revision on a palette draft saved before drafts named one, then maps it', async () => {
    upscaleLineage()
    localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ palette: { protected_component_points: [[10, 20]] } }))
    window.history.replaceState(null, '', `/#stage=flatten&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(storedPaletteDraft().palette_revision).toBe('palette-1'))
    await vi.waitFor(() => expect(workspaceProps.current?.markers?.map(marker => marker.points)).toEqual([[[10, 20]]]))

    showRevision('upscaled-1')
    await expectUpscaledRunMapped()
  })

  it('draws preservation markers only once the shown artifacts are the selected revision\'s', async () => {
    upscaleLineage()
    localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ palette: { protected_component_points: [[10, 20]] }, palette_revision: 'palette-1' }))
    let releaseChild: (rows: Artifact[]) => void = () => undefined
    mocks.effectiveArtifacts.mockImplementation((id: string) => id === 'upscaled-1'
      ? new Promise<Artifact[]>(resolve => { releaseChild = resolve }) : Promise.resolve([artifact('source')]))
    window.history.replaceState(null, '', `/#stage=flatten&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(workspaceProps.current?.markers?.map(marker => marker.points)).toEqual([[[10, 20]]]))

    showRevision('upscaled-1')
    // The points are already on the child's grid while the parent's source is still shown.
    await vi.waitFor(() => expect(mocks.effectiveArtifacts).toHaveBeenCalledWith('upscaled-1', expect.anything()))
    expect(workspaceProps.current?.markers).toBeUndefined()
    await act(async () => releaseChild([artifact('source')]))
    await vi.waitFor(() => expect(workspaceProps.current?.markers?.map(marker => marker.points)).toEqual([[[22, 44]]]))
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
    await vi.waitFor(() => expect(screen.getByLabelText('Current work').textContent).toContain('Sky whale'))
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
  // A submitted generation selects its new project, whose load briefly swaps the job
  // overlay for the loading overlay (same element, new text). Once that load has fetched
  // its artifacts the loading overlay has already rendered, so the job overlay showing
  // then is the settled one; reading it any earlier can catch the pre-load render.
  async function settledJobOverlay(message = 'Preparing artwork') {
    await vi.waitFor(() => {
      expect(mocks.effectiveArtifacts).toHaveBeenCalled()
      const statuses = screen.getAllByRole('status')
      expect(statuses).toHaveLength(1)
      expect(statuses[0].textContent).toContain(message)
      expect(screen.queryByRole('progressbar', { name: 'Job progress' })).not.toBeNull()
    })
    return screen.getByRole('progressbar', { name: 'Job progress' })
  }

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

    const progress = await settledJobOverlay()
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
    const progress = await settledJobOverlay()
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

  it('shows an advancing, screen-reader-hidden elapsed timer during a job', async () => {
    vi.useFakeTimers()
    try {
      mocks.generate.mockResolvedValue({ project: { ...project, revisions: [revision] }, job: queuedJob, expanded_prompt: 'Expanded' })
      render(createElement((await import('./App')).default))
      fireEvent.change(screen.getByLabelText('Short subject'), { target: { value: 'sky whale' } })
      fireEvent.submit(screen.getByLabelText('Short subject').closest('form')!)
      // Flush the generate promise so the job (and its overlay) mount under fake timers.
      await act(async () => { await Promise.resolve() })

      const timer = document.querySelector('.canvas-loading .elapsed') as HTMLElement
      expect(timer).not.toBeNull()
      expect(timer.getAttribute('aria-hidden')).toBe('true')
      expect(timer.textContent).toBe('0:00')

      act(() => vi.advanceTimersByTime(2000))
      expect(timer.textContent).toBe('0:02')
      // The ticking timer must not introduce a second polite live region.
      expect(screen.getAllByRole('status')).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('announces cancellation and completion through the same atomic status', async () => {
    let update: ((job: Job) => void) | undefined
    mocks.followJob.mockImplementation((_id, onJob) => { update = onJob; return () => undefined })
    mocks.generate.mockResolvedValue({ project: { ...project, revisions: [revision] }, job: queuedJob, expanded_prompt: 'Expanded' })
    render(createElement((await import('./App')).default))
    fireEvent.change(screen.getByLabelText('Short subject'), { target: { value: 'sky whale' } })
    fireEvent.submit(screen.getByLabelText('Short subject').closest('form')!)
    await settledJobOverlay()

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
    await settledJobOverlay()

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
    await chooseProject()
    fireEvent.click(await screen.findByRole('button', { name: 'Save SVG to shared folder' }))

    const status = (await screen.findByText('Saved traced-overlap.svg to the shared folder.')).closest('[role="status"]')!
    expect(status).not.toBeNull()
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(screen.getAllByRole('status')).toEqual([status])
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
    await chooseProject()

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
    await chooseProject()
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
    await chooseProject()
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

describe('upload size limit', () => {
  it('rejects an image over the server limit with a clear message before uploading', async () => {
    mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 4 })
    render(createElement((await import('./App')).default))
    // Wait until the fetched limit has replaced the default cap.
    await screen.findByText(/Up to 0.0 MB\./)

    const input = screen.getByLabelText(/Source image/) as HTMLInputElement
    const oversize = new File(['larger-than-four-bytes'], 'big.png', { type: 'image/png' })
    fireEvent.change(input, { target: { files: [oversize] } })
    fireEvent.submit(input.closest('form')!)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('over the 0.0 MB upload limit')
    expect(mocks.importProject).not.toHaveBeenCalled()
  })

  it('uploads an image within the limit', async () => {
    render(createElement((await import('./App')).default))
    const input = screen.getByLabelText(/Source image/) as HTMLInputElement
    const file = new File(['small'], 'small.png', { type: 'image/png' })
    fireEvent.change(input, { target: { files: [file] } })
    fireEvent.submit(input.closest('form')!)

    await vi.waitFor(() => expect(mocks.importProject).toHaveBeenCalled())
    expect(mocks.importProject.mock.calls[0][1]).toBe(file)
  })

  it('shows the shared overlay with transfer progress then processing during upload', async () => {
    let reportProgress: ((fraction: number) => void) | undefined
    let finishImport: (() => void) | undefined
    mocks.importProject.mockImplementation((_name: string, _file: File, onProgress?: (fraction: number) => void) => {
      reportProgress = onProgress
      return new Promise<Project & { revisions: Revision[] }>(resolve => {
        finishImport = () => resolve({ ...project, revisions: [revision] })
      })
    })
    render(createElement((await import('./App')).default))
    const input = screen.getByLabelText(/Source image/) as HTMLInputElement
    const file = new File(['artwork'], 'art.png', { type: 'image/png' })
    fireEvent.change(input, { target: { files: [file] } })
    fireEvent.submit(input.closest('form')!)

    // Determinate transfer in the shared overlay, alongside the elapsed timer.
    await screen.findByText('Uploading image… 0%')
    expect(mocks.importProject.mock.calls[0][1]).toBe(file)
    expect(document.querySelector('.canvas-loading .elapsed')).not.toBeNull()

    act(() => reportProgress?.(0.5))
    expect(screen.getByText('Uploading image… 50%')).toBeTruthy()
    expect(screen.getByRole('progressbar', { name: 'Upload progress' }).getAttribute('aria-valuenow')).toBe('50')
    // Upload is not a job; its bar announces as transfer progress, not "Job progress".
    expect(screen.queryByRole('progressbar', { name: 'Job progress' })).toBeNull()
    // Exactly one polite region — the overlay message — through the whole upload.
    expect(screen.getAllByRole('status')).toHaveLength(1)

    // Once the bytes are sent, the overlay switches to indeterminate processing.
    act(() => reportProgress?.(1))
    expect(screen.getByText('Processing image…')).toBeTruthy()
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBeNull()
    expect(document.querySelector('.spinner')).not.toBeNull()

    await act(async () => { finishImport?.(); await new Promise(resolve => setTimeout(resolve)) })
  })

  it('keeps a single polite status when an upload starts after a completed job', async () => {
    // A finished job leaves its result banner (role="status") on screen; starting an
    // upload must not add a second polite region alongside the overlay's own status.
    mocks.generate.mockResolvedValue({ project: { ...project, revisions: [revision] }, job: queuedJob, expanded_prompt: 'Expanded' })
    mocks.followJob.mockImplementation((_id: string, onJob: (job: Job) => void) => {
      onJob({ ...queuedJob, kind: 'generate', status: 'succeeded', progress: 1, message: 'Artwork ready', result_revision_id: revision.id })
      return () => undefined
    })
    let reportProgress: ((fraction: number) => void) | undefined
    mocks.importProject.mockImplementation((_name: string, _file: File, onProgress?: (fraction: number) => void) => {
      reportProgress = onProgress
      return new Promise<Project & { revisions: Revision[] }>(() => { /* stays pending on the upload overlay */ })
    })
    render(createElement((await import('./App')).default))
    fireEvent.change(screen.getByLabelText('Short subject'), { target: { value: 'sky whale' } })
    fireEvent.submit(screen.getByLabelText('Short subject').closest('form')!)
    // The completed job's result banner is the only status before the upload.
    await vi.waitFor(() => expect(screen.getByRole('status').textContent).toContain('Artwork ready'))

    const input = screen.getByLabelText(/Source image/) as HTMLInputElement
    const file = new File(['artwork'], 'art.png', { type: 'image/png' })
    fireEvent.change(input, { target: { files: [file] } })
    fireEvent.submit(input.closest('form')!)
    act(() => reportProgress?.(0.5))

    await screen.findByText('Uploading image… 50%')
    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(screen.getByRole('status').textContent).toContain('Uploading image…')
  })
})

describe('landing on the newly produced artwork', () => {
  const paletteRevision: Revision = { ...revision, id: 'palette-1', parent_revision_id: 'revision-1', kind: 'palette' }
  const traceRevision: Revision = { ...revision, id: 'trace-1', parent_revision_id: 'palette-1', kind: 'trace', settings: { trace: { overlap: 0, solid_underlayers: false } } }
  const editRevision: Revision = { ...revision, id: 'edit-1', parent_revision_id: 'palette-1', kind: 'label-edit' }
  const pressedView = (name: string) => within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).getByRole('button', { name }).getAttribute('aria-pressed')

  function followWithUpdates() {
    let update: ((job: Job) => void) | undefined
    mocks.followJob.mockImplementation((_id: string, onJob: (job: Job) => void) => { update = onJob; return () => undefined })
    return (job: Job) => act(async () => update?.(job))
  }

  it('shows the flattened image once a flatten run succeeds', async () => {
    const finish = followWithUpdates()
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValueOnce({ ...project, revisions: [revision] })
      .mockResolvedValue({ ...project, revisions: [paletteRevision, revision] })
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => id === 'palette-1'
      ? [artifact('source'), artifact('flattened')] : [artifact('source')])
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    const run = screen.getByRole('button', { name: 'Flatten image' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    await vi.waitFor(() => expect(mocks.followJob).toHaveBeenCalled())

    await finish({ ...queuedJob, kind: 'palette', status: 'succeeded', progress: 1, result_revision_id: 'palette-1' })
    await vi.waitFor(() => expect(pressedView('Flattened')).toBe('true'))
  })

  it('traces on the shared canvas and shows the traced SVG once a trace run succeeds', async () => {
    const finish = followWithUpdates()
    const svg = artifact('traced-edge', 'image/svg+xml')
    const overlapSvg = artifact('traced-overlap', 'image/svg+xml')
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValueOnce({ ...project, revisions: [paletteRevision, revision] })
      .mockResolvedValue({ ...project, revisions: [traceRevision, paletteRevision, revision] })
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => id === 'trace-1'
      ? [artifact('source'), artifact('flattened'), artifact('segmentation', 'application/octet-stream'), overlapSvg, svg, artifact('traced-preview')]
      : [artifact('source'), artifact('flattened'), artifact('segmentation', 'application/octet-stream')])
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(workspaceProps.current?.editorMode).toBe(true))
    expect(workspaceProps.current?.canvasLabel).toBe('Trace canvas')
    await screen.findAllByRole('group', { name: 'Artwork view' })
    fireEvent.click(within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).getByRole('button', { name: 'Source' }))
    await vi.waitFor(() => expect(pressedView('Source')).toBe('true'))

    const run = screen.getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    await vi.waitFor(() => expect(mocks.followJob).toHaveBeenCalled())
    await finish({ ...queuedJob, kind: 'trace', status: 'succeeded', progress: 1, result_revision_id: 'trace-1' })

    await vi.waitFor(() => expect(pressedView('Traced')).toBe('true'))
    // The Traced view renders the variant the run produced (edge to edge with no overlap
    // or solid underlayers), not the PNG preview.
    expect(workspaceProps.current?.preferredLayerId).toBe(svg.id)
    expect(workspaceProps.current?.layers?.find(layer => layer.id === svg.id)?.name).toBe('Traced SVG · edge to edge')
    // Traced SVGs register on the flattened pixel grid; rasters keep their own size.
    expect(workspaceProps.current?.layers?.find(layer => layer.id === svg.id)?.sizeFrom).toBe('flattened-id')
    expect(workspaceProps.current?.layers?.find(layer => layer.id === 'source-id')?.sizeFrom).toBeUndefined()
  })

  it('keeps the Palette canvas on the editable image when a trace finishes there', async () => {
    const finish = followWithUpdates()
    const svg = artifact('traced-edge', 'image/svg+xml')
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValueOnce({ ...project, revisions: [paletteRevision, revision] })
      .mockResolvedValue({ ...project, revisions: [traceRevision, paletteRevision, revision] })
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => id === 'trace-1'
      ? [artifact('source'), artifact('flattened'), artifact('segmentation', 'application/octet-stream'), svg]
      : [artifact('source'), artifact('flattened'), artifact('segmentation', 'application/octet-stream')])
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    const run = await screen.findByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    await vi.waitFor(() => expect(mocks.followJob).toHaveBeenCalled())
    // The user moves on to Palette while the trace is still running.
    fireEvent.click(screen.getByRole('tab', { name: 'Palette' }))
    await finish({ ...queuedJob, kind: 'trace', status: 'succeeded', progress: 1, result_revision_id: 'trace-1' })

    await vi.waitFor(() => expect(pressedView('Traced')).toBe('false'))
    expect(pressedView('Flattened')).toBe('true')
    expect(workspaceProps.current?.preferredLayerId).toBe('flattened-id')
  })

  it('shows the overlap SVG for a run with solid underlayers', async () => {
    const solidRevision: Revision = { ...traceRevision, settings: { trace: { overlap: 0, solid_underlayers: true } } }
    const edge = artifact('traced-edge', 'image/svg+xml'), overlap = artifact('traced-overlap', 'image/svg+xml')
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [solidRevision, paletteRevision, revision] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened'), edge, overlap])
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-1`)
    render(createElement((await import('./App')).default))
    await screen.findAllByRole('group', { name: 'Artwork view' })
    fireEvent.click(within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).getByRole('button', { name: 'Traced' }))
    await vi.waitFor(() => expect(workspaceProps.current?.preferredLayerId).toBe(overlap.id))
    expect(workspaceProps.current?.layers?.find(layer => layer.id === overlap.id)?.name).toBe('Traced SVG · solid underlayers')
  })

  it('shows the edited image once a palette edit commits', async () => {
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValueOnce({ ...project, revisions: [paletteRevision, revision] })
      .mockResolvedValue({ ...project, revisions: [editRevision, paletteRevision, revision] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened')])
    window.history.replaceState(null, '', `/#stage=palette&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(editorProps.current).not.toBeNull())
    act(() => editorProps.current!.onCommit('edit-1'))
    await vi.waitFor(() => expect(pressedView('Edited')).toBe('true'))
  })
})

describe('project deletion', () => {
  it('deletes only after the confirmation dialog is accepted', async () => {
    mocks.projects.mockResolvedValue([project])
    render(createElement((await import('./App')).default))
    await chooseProject()

    fireEvent.click(screen.getByRole('button', { name: 'Delete project' }))
    const dialog = screen.getByRole('alertdialog', { name: 'Delete project?' })
    expect(dialog.textContent).toContain('Sky whale')
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Cancel' }))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(mocks.deleteProject).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Delete project' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Delete project' }))
    await vi.waitFor(() => expect(mocks.deleteProject).toHaveBeenCalledWith(project.id))
    await vi.waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect((screen.getByLabelText('Active project') as HTMLSelectElement).value).toBe('')
  })
})

describe('stepping through trace runs', () => {
  const paletteRevision: Revision = { ...revision, id: 'palette-1', kind: 'palette', settings: { palette: {} } }
  const run1: Revision = { ...revision, id: 'trace-1', parent_revision_id: 'palette-1', kind: 'trace', created_at: '2026-01-02', settings: { trace: { overlap: 0, solid_underlayers: true } } }
  const run2: Revision = { ...revision, id: 'trace-2', parent_revision_id: 'trace-1', kind: 'trace', created_at: '2026-01-03', settings: { trace: { overlap: 0, solid_underlayers: false } } }
  const svgs = (run: string) => [
    { ...artifact('traced-edge', 'image/svg+xml'), id: `${run}-edge` },
    { ...artifact('traced-overlap', 'image/svg+xml'), id: `${run}-overlap` },
  ]

  it('makes each run the active revision, shows its SVG, and keeps the draft on the newest run', async () => {
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [run2, run1, paletteRevision, revision] })
    mocks.effectiveArtifacts.mockImplementation(async (id: string) =>
      [artifact('source'), artifact('flattened'), artifact('segmentation', 'application/octet-stream'), ...svgs(id)])
    localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { corner_threshold: 45 } }))
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-2`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Trace' })
    await within(panel).findByText('Iteration 2 of 2')
    const solid = within(panel).getByRole('checkbox', { name: /Solid underlayers/ }) as HTMLInputElement
    const corner = within(panel).getByLabelText(/Corner threshold/) as HTMLInputElement
    await vi.waitFor(() => expect(corner.value).toBe('45'))

    fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
    await within(panel).findByText('Iteration 1 of 2')
    expect(window.location.hash).toContain('revision=trace-1')
    // The older run's own traced SVG, its own variant, and exactly its own settings.
    await vi.waitFor(() => expect(workspaceProps.current?.preferredLayerId).toBe('trace-1-overlap'))
    expect(solid.checked).toBe(true)
    expect(corner.value).toBe('60')

    fireEvent.click(within(panel).getByLabelText('Show the next trace run'))
    await within(panel).findByText('Iteration 2 of 2')
    expect(window.location.hash).toContain('revision=trace-2')
    await vi.waitFor(() => expect(workspaceProps.current?.preferredLayerId).toBe('trace-2-edge'))
    expect(solid.checked).toBe(false)
    expect(corner.value).toBe('45')
    expect((within(panel).getByLabelText('Show the next trace run') as HTMLButtonElement).disabled).toBe(true)
  })

  async function openAtNewestRun() {
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [run2, run1, paletteRevision, revision] })
    mocks.effectiveArtifacts.mockImplementation(async (id: string) =>
      [artifact('source'), artifact('flattened'), artifact('segmentation', 'application/octet-stream'), ...svgs(id)])
    localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { corner_threshold: 45 } }))
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-2`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Trace' })
    await within(panel).findByText('Iteration 2 of 2')
    const corner = within(panel).getByLabelText(/Corner threshold/) as HTMLInputElement
    await vi.waitFor(() => expect(corner.value).toBe('45'))
    return { panel, corner }
  }
  const storedDraft = () => JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) || '{}').trace

  it('never turns an older run\'s settings into the newest run\'s draft when focus leaves the form', async () => {
    const { panel, corner } = await openAtNewestRun()
    const back = within(panel).getByLabelText('Show the previous trace run')
    fireEvent.click(back)
    await within(panel).findByText('Iteration 1 of 2')
    await vi.waitFor(() => expect(corner.value).toBe('60'))
    // Focus leaving the stepper or an edited field at the older run commits no draft.
    fireEvent.focusOut(back)
    fireEvent.change(corner, { target: { value: '70' } })
    fireEvent.focusOut(corner)
    expect(storedDraft()).toEqual({ corner_threshold: 45 })

    fireEvent.click(within(panel).getByLabelText('Show the next trace run'))
    await within(panel).findByText('Iteration 2 of 2')
    await vi.waitFor(() => expect(corner.value).toBe('45'))
  })

  it('keeps the newest run\'s draft when a trace branches from an older run', async () => {
    let update: ((job: Job) => void) | undefined
    mocks.followJob.mockImplementation((_id: string, onJob: (job: Job) => void) => { update = onJob; return () => undefined })
    mocks.startPipeline.mockResolvedValue({ ...queuedJob, id: 'job-branch', kind: 'trace' })
    const { panel } = await openAtNewestRun()
    fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
    await within(panel).findByText('Iteration 1 of 2')
    const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    await vi.waitFor(() => expect(update).toBeDefined())
    await act(async () => update?.({ ...queuedJob, id: 'job-branch', kind: 'trace', status: 'succeeded', progress: 1, result_revision_id: 'trace-1' }))
    expect(storedDraft()).toEqual({ corner_threshold: 45 })
  })

  it('spends the draft when the newest run is traced again', async () => {
    let update: ((job: Job) => void) | undefined
    mocks.followJob.mockImplementation((_id: string, onJob: (job: Job) => void) => { update = onJob; return () => undefined })
    mocks.startPipeline.mockResolvedValue({ ...queuedJob, id: 'job-tip', kind: 'trace' })
    const { panel } = await openAtNewestRun()
    const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    expect(mocks.startPipeline).toHaveBeenCalledWith('trace', project.id, 'trace-2', { trace: expect.objectContaining({ corner_threshold: 45 }) })
    await vi.waitFor(() => expect(update).toBeDefined())
    await act(async () => update?.({ ...queuedJob, id: 'job-tip', kind: 'trace', status: 'succeeded', progress: 1, result_revision_id: 'trace-2' }))
    expect(storedDraft()).toBeUndefined()
  })

  it('flags the canvas as loading while a stepped run\'s artifacts are fetched, keeping the shown layers', async () => {
    const pending: Record<string, (rows: Artifact[]) => void> = {}
    await openAtNewestRun()
    mocks.effectiveArtifacts.mockImplementation((id: string) => new Promise<Artifact[]>(resolve => { pending[id] = resolve }))
    await vi.waitFor(() => expect(workspaceProps.current?.loading).toBe(false))
    const panel = screen.getByRole('tabpanel', { name: 'Trace' })
    fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
    await vi.waitFor(() => expect(pending['trace-1']).toBeDefined())
    expect(workspaceProps.current?.loading).toBe(true)
    // The newest run's layers stay on the canvas under the spinner.
    expect(workspaceProps.current?.layers?.some(layer => layer.id === 'trace-2-edge')).toBe(true)
    await act(async () => pending['trace-1']([artifact('source'), artifact('flattened'), ...svgs('trace-1')]))
    await vi.waitFor(() => expect(workspaceProps.current?.loading).toBe(false))
    expect(workspaceProps.current?.preferredLayerId).toBe('trace-1-overlap')
  })

  it('offers the transparent audit overlays as labelled toggles and never the opaque audit images', async () => {
    const audits = [
      { ...artifact('trace-fallbacks'), id: 'opaque-fallbacks' },
      { ...artifact('trace-semantic-warnings'), id: 'opaque-semantic' },
      { ...artifact('trace-fallbacks-overlay'), id: 'fallbacks-overlay' },
      { ...artifact('trace-missed-details-overlay'), id: 'missed-overlay' },
    ]
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [run2, run1, paletteRevision, revision] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened'), ...svgs('trace-2'), ...audits])
    mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-2`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(workspaceProps.current?.canvasLabel).toBe('Trace canvas'))
    await vi.waitFor(() => expect(workspaceProps.current?.layers?.some(layer => layer.id === 'missed-overlay')).toBe(true))
    const offered = (layers: CapturedWorkspace['layers']) => (layers ?? []).filter(layer => layer.role === 'overlay').map(layer => layer.name).sort()
    const ids = () => (workspaceProps.current?.layers ?? []).map(layer => layer.id)
    expect(offered(workspaceProps.current?.layers)).toEqual(['Missed source details (red)', 'Tightened boundaries'])
    expect(ids()).not.toContain('opaque-fallbacks')
    expect(ids()).not.toContain('opaque-semantic')

    // Export's canvas and its inspector build on the same layers.
    workspaceProps.current = null
    fireEvent.click(screen.getByRole('tab', { name: 'Export' }))
    const exportImages = [...document.querySelectorAll('.artwork-preview img')].map(node => node.getAttribute('src'))
    expect(exportImages).not.toContain('/artifacts/trace-fallbacks')
    expect(exportImages).not.toContain('/artifacts/trace-semantic-warnings')
    fireEvent.click(screen.getByRole('button', { name: 'Inspect image' }))
    await vi.waitFor(() => expect(workspaceProps.current).not.toBeNull())
    expect(ids()).not.toContain('opaque-fallbacks')
    expect(ids()).not.toContain('opaque-semantic')
    expect(offered((workspaceProps.current as CapturedWorkspace | null)?.layers)).toEqual(['Missed source details (red)', 'Tightened boundaries'])
  })

})

describe('cleanup stage', () => {
  const stale = ['cleanup', 'trace', 'validation', 'export']
  const paletteRevision: Revision = { ...revision, id: 'palette-1', parent_revision_id: 'revision-1', kind: 'palette', settings: { palette: {}, stale_concerns: stale } }
  const cleanupRevision: Revision = { ...revision, id: 'cleanup-1', parent_revision_id: 'palette-1', kind: 'cleanup', created_at: '2026-01-02',
    settings: { cleanup: { width_mm: 300, height_mm: null, remove_colour: null, cleanup: true, cleanup_action: 'remove', min_detail_width: .4, min_region_area: 0, remove_below_width: 0 }, stale_concerns: ['trace', 'validation', 'export'] } }
  const segmentation = () => artifact('segmentation', 'application/octet-stream')
  const cleanedFlattened = { ...artifact('flattened'), id: 'flattened-cleaned', revision_id: 'cleanup-1', download_url: '/artifacts/flattened-cleaned' }
  const pressedView = (name: string) => within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).getByRole('button', { name }).getAttribute('aria-pressed')

  function serve(revisions: Revision[]) {
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions })
    // The cleanup revision's raster is its own cleaned one; everything else inherits the
    // palette revision's flattened image.
    // A before-cleanup lookup resolves the raster the cleanup started from.
    mocks.effectiveArtifacts.mockImplementation(async (id: string, _signal?: AbortSignal, options?: { beforeCleanup?: boolean }) => [artifact('source'), segmentation(),
      id === 'palette-1' || id === 'revision-1' || id.startsWith('palette-') || options?.beforeCleanup || id === 'cleanup-off' ? artifact('flattened') : cleanedFlattened])
    mocks.editor.mockResolvedValue({ width: 600, height: 400, protected_pixels: 0, palette: [] })
  }

  it('sits between Edit and Trace with the Lucide eraser icon', async () => {
    render(createElement((await import('./App')).default))
    const tabs = screen.getAllByRole('tab').map(tab => tab.getAttribute('aria-label'))
    expect(tabs.slice(tabs.indexOf('Edit'), tabs.indexOf('Edit') + 3)).toEqual(['Edit', 'Cleanup', 'Trace'])
    expect(screen.getByRole('tab', { name: 'Cleanup' }).querySelector('svg.lucide-eraser')).toBeTruthy()
  })

  it('renders the shared canvas for the Cleanup stage, offering the cleaned and pre-cleanup rasters', async () => {
    serve([cleanupRevision, paletteRevision, revision])
    window.history.replaceState(null, '', `/#stage=cleanup&project=${project.id}&revision=cleanup-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(workspaceProps.current?.editorMode).toBe(true))
    expect(workspaceProps.current?.canvasLabel).toBe('Cleanup canvas')
    await vi.waitFor(() => expect(workspaceProps.current?.layers?.some(layer => layer.id === 'flattened-cleaned')).toBe(true))
    expect(workspaceProps.current?.layers?.some(layer => layer.id === 'flattened-id')).toBe(true)
  })

  it('renders the zoomable shared canvas for the Upload and Flatten stages, not the static preview', async () => {
    serve([paletteRevision, revision])
    window.history.replaceState(null, '', `/#stage=flatten&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(workspaceProps.current?.editorMode).toBe(true))
    expect(workspaceProps.current?.canvasLabel).toBe('Flatten canvas')
    await vi.waitFor(() => expect(workspaceProps.current?.layers?.some(layer => layer.id === 'flattened-id')).toBe(true))
    expect(workspaceProps.current?.layers?.some(layer => layer.id === 'source-id')).toBe(true)
    expect(document.querySelector('.artwork-preview')).toBeNull()
    expect(workspaceProps.current?.onSelectPoint).toBeUndefined()
    // Picking a preservation point stays on the same Flatten canvas, with clicks routed to it.
    fireEvent.click(screen.getByRole('button', { name: /Protect connected detail/ }))
    await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeTypeOf('function'))
    expect(workspaceProps.current?.canvasLabel).toBe('Flatten canvas')
    fireEvent.click(screen.getByRole('tab', { name: 'Upload' }))
    await vi.waitFor(() => expect(workspaceProps.current?.canvasLabel).toBe('Upload canvas'))
    expect(workspaceProps.current?.editorMode).toBe(true)
    expect(workspaceProps.current?.onSelectPoint).toBeUndefined()
    expect(workspaceProps.current?.layers?.some(layer => layer.id === 'source-id')).toBe(true)
    expect(document.querySelector('.artwork-preview')).toBeNull()
  })

  it('runs cleanup with the form settings, spends its draft, and lands on the Cleaned view', async () => {
    let update: ((job: Job) => void) | undefined
    mocks.followJob.mockImplementation((_id: string, onJob: (job: Job) => void) => { update = onJob; return () => undefined })
    mocks.startPipeline.mockResolvedValue({ ...queuedJob, id: 'job-cleanup', kind: 'cleanup' })
    serve([paletteRevision, revision])
    localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ cleanup: { width_mm: 300, cleanup: true }, trace: { overlap: 4 } }))
    window.history.replaceState(null, '', `/#stage=cleanup&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Cleanup' })
    const width = within(panel).getByLabelText(/Width \(mm\)/) as HTMLInputElement
    await vi.waitFor(() => expect(width.value).toBe('300'))
    // The draft differs from the (default) baseline, so it carries a pending dot.
    expect(width.closest('label')?.querySelector('.field-dirty')).toBeTruthy()
    expect(within(panel).getByText('Minimum detail width (mm)')).toBeTruthy()
    const run = within(panel).getByRole('button', { name: 'Run cleanup' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    expect(mocks.startPipeline).toHaveBeenCalledWith('cleanup', project.id, 'palette-1', { cleanup: expect.objectContaining({ width_mm: 300, cleanup: true, cleanup_action: 'remove' }) })

    serve([cleanupRevision, paletteRevision, revision])
    await vi.waitFor(() => expect(update).toBeDefined())
    const result = 'Cleanup changed 40 px; removed 2 automatically protected details (11 px); kept 1 explicitly protected regions under the threshold'
    await act(async () => update?.({ ...queuedJob, id: 'job-cleanup', kind: 'cleanup', status: 'succeeded', progress: 1, message: result, result_revision_id: 'cleanup-1' }))
    await vi.waitFor(() => expect(pressedView('Cleaned')).toBe('true'))
    // The job result states what the cleanup did to protected detail.
    expect(screen.getByText(result)).toBeTruthy()
    expect(window.location.hash).toContain('revision=cleanup-1')
    await vi.waitFor(() => expect(within(panel).getByText('Iteration 1 of 1')).toBeTruthy())
    const stored = JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) || '{}')
    expect(stored.cleanup).toBeUndefined()
    expect(stored.trace).toEqual({ overlap: 4 })
    // The recorded cleanup is now the baseline, so nothing is pending.
    expect(width.closest('label')?.querySelector('.field-dirty')).toBeNull()
  })

  describe('the removed colour after a trace renamed it', () => {
    // Cleanup removed Orange (#E15F2D), recorded by its then name 'Sand'; a trace then
    // swapped the names, so this palette calls Navy 'Sand' and #E15F2D 'Orange'.
    const renamedCleanup: Revision = { ...cleanupRevision, settings: { ...cleanupRevision.settings,
      cleanup: { ...(cleanupRevision.settings.cleanup as object), remove_colour: 'Sand' }, removed_colour_index: 1, removed_colour_hex: '#E15F2D' } }
    const renamedTrace: Revision = { ...revision, id: 'trace-1', parent_revision_id: 'cleanup-1', kind: 'trace', created_at: '2026-01-03', settings: { trace: {}, stale_concerns: ['validation', 'export'] } }
    type Colour = { index: number; name: string; hex: string; rgb: [number, number, number]; share_percent: number }
    const swapped: Colour[] = [
      { index: 0, name: 'Sand', hex: '#0F233C', rgb: [15, 35, 60], share_percent: 60 },
      { index: 1, name: 'Orange', hex: '#E15F2D', rgb: [225, 95, 45], share_percent: 40 },
    ]
    const draftOf = () => JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) || '{}')
    const preview = () => (workspaceProps.current as { cleanupPreview?: { settings: { remove_colour: string | null } } } | null)?.cleanupPreview
    // `inherited` is the server's resolution for trace-1 (EditorState.inherited_remove_colour).
    async function openCleanup(palette: Colour[], inherited: { value: string | null; unresolved: boolean; hex?: string; problem?: 'missing' | 'ambiguous' | null } | null = { value: '#E15F2D', unresolved: false, hex: '#E15F2D', problem: null }) {
      serve([renamedTrace, renamedCleanup, paletteRevision, revision])
      mocks.editor.mockResolvedValue({ width: 600, height: 400, protected_pixels: 0, palette, inherited_remove_colour: inherited })
      window.history.replaceState(null, '', `/#stage=cleanup&project=${project.id}&revision=trace-1`)
      render(createElement((await import('./App')).default))
      return screen.getByRole('tabpanel', { name: 'Cleanup' })
    }
    async function runsWith(panel: HTMLElement, value: string) {
      mocks.startPipeline.mockResolvedValue({ ...queuedJob, id: 'job-cleanup', kind: 'cleanup' })
      await vi.waitFor(() => expect(preview()?.settings.remove_colour).toBe(value))
      const run = within(panel).getByRole('button', { name: 'Run cleanup' }) as HTMLButtonElement
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      fireEvent.click(run)
      await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalledWith('cleanup', project.id, 'trace-1', { cleanup: expect.objectContaining({ remove_colour: value }) }))
    }

    it('shows the colour Cleanup removed under its current name, and previews and runs it by hex', async () => {
      const panel = await openCleanup(swapped)
      const colour = within(panel).getByLabelText(/Remove colour/) as HTMLInputElement
      await vi.waitFor(() => expect(colour.value).toBe('#E15F2D'))
      await vi.waitFor(() => expect(panel.textContent).toContain('Removes Orange (#E15F2D)'))
      // The note sits outside the field's label and describes the input.
      const note = panel.querySelector('#remove-colour-status')!
      expect(note.closest('label')).toBeNull()
      expect(colour.getAttribute('aria-describedby')).toBe('remove-colour-status')
      // The inherited value is the recorded cleanup's, so it is not a pending edit.
      expect(colour.closest('label')?.querySelector('.field-dirty')).toBeNull()
      await runsWith(panel, '#E15F2D')
    })

    it('takes the name the server resolves when the removed colour\'s hex is two colours\' now', async () => {
      // A recolour gave Navy the removed colour's RGB before this (fresh) cleanup, so the
      // hex is ambiguous and the server names the colour by its current name instead.
      const duplicate: Colour[] = [{ ...swapped[0], hex: '#E15F2D', rgb: [225, 95, 45] }, swapped[1]]
      const panel = await openCleanup(duplicate, { value: 'Orange', unresolved: false })
      const colour = within(panel).getByLabelText(/Remove colour/) as HTMLInputElement
      await vi.waitFor(() => expect(colour.value).toBe('Orange'))
      expect(panel.textContent).toContain('Removes Orange (#E15F2D)')
      expect(colour.closest('label')?.querySelector('.field-dirty')).toBeNull()
      expect(within(panel).queryByRole('alert')).toBeNull()
      await runsWith(panel, 'Orange')
    })

    it('blocks Cleanup on the unresolved inherited colour until the field is edited', async () => {
      // The last cleanup's colour was recoloured away: the server resolves no colour, so
      // the form keeps its hex, and neither a Run nor a preview removes nothing silently.
      const recoloured: Colour[] = [swapped[0], { ...swapped[1], hex: '#0CC85A', rgb: [12, 200, 90] }]
      const panel = await openCleanup(recoloured, { value: null, unresolved: true, hex: '#E15F2D', problem: 'missing' })
      const reason = "The colour the last Cleanup removed (#E15F2D) is not in this artwork's palette any more (it was re-flattened, recoloured or merged). Choose the colour to remove, or clear the field to remove none."
      const colour = within(panel).getByLabelText(/Remove colour/) as HTMLInputElement
      await vi.waitFor(() => expect(within(panel).getByRole('alert').textContent).toBe(reason))
      expect(colour.value).toBe('#E15F2D')
      expect(colour.getAttribute('aria-invalid')).toBe('true')
      expect(colour.closest('label')?.querySelector('.field-dirty')).toBeNull()
      const run = within(panel).getByRole('button', { name: 'Run cleanup' }) as HTMLButtonElement
      expect(run.disabled).toBe(true)
      expect(document.getElementById(run.getAttribute('aria-describedby')!.split(' ').at(-1)!)?.textContent).toBe(reason)
      // The live preview is paused with the reason, so the canvas requests nothing.
      const canvas = () => workspaceProps.current as { livePreviewPaused?: boolean; livePreviewStatus?: string } | null
      expect(canvas()?.livePreviewPaused).toBe(true)
      expect(canvas()?.livePreviewStatus).toBe(reason)
      // Choosing a colour that resolves unblocks both.
      fireEvent.change(colour, { target: { value: 'Orange' } })
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      expect(within(panel).queryByRole('alert')).toBeNull()
      expect(canvas()?.livePreviewPaused).toBe(false)
      expect(preview()?.settings.remove_colour).toBe('Orange')
      // Clearing it removes nothing, deliberately: that runs too.
      fireEvent.change(colour, { target: { value: '' } })
      await vi.waitFor(() => expect(preview()?.settings.remove_colour).toBeNull())
      expect(run.disabled).toBe(false)
      mocks.startPipeline.mockResolvedValue({ ...queuedJob, id: 'job-cleanup', kind: 'cleanup' })
      fireEvent.click(run)
      await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalledWith('cleanup', project.id, 'trace-1', { cleanup: expect.objectContaining({ remove_colour: null }) }))
    })

    it('says an ambiguous inherited colour matches more than one colour', async () => {
      const panel = await openCleanup(swapped, { value: null, unresolved: true, hex: '#E15F2D', problem: 'ambiguous' })
      await vi.waitFor(() => expect(within(panel).getByRole('alert').textContent).toBe(
        "The colour the last Cleanup removed (#E15F2D) now matches more than one colour of this artwork's palette, by hex or by name. Choose the colour to remove, or clear the field to remove none."))
      expect((within(panel).getByRole('button', { name: 'Run cleanup' }) as HTMLButtonElement).disabled).toBe(true)
    })

    it('holds Run until this revision\'s inherited colour is checked', async () => {
      let answer: (() => void) | undefined
      serve([renamedTrace, renamedCleanup, paletteRevision, revision])
      mocks.editor.mockImplementation(() => new Promise(resolve => { answer = () => resolve({ width: 600, height: 400, protected_pixels: 0, palette: swapped, inherited_remove_colour: { value: '#E15F2D', unresolved: false, hex: '#E15F2D', problem: null } }) }))
      window.history.replaceState(null, '', `/#stage=cleanup&project=${project.id}&revision=trace-1`)
      render(createElement((await import('./App')).default))
      const panel = screen.getByRole('tabpanel', { name: 'Cleanup' })
      const run = within(panel).getByRole('button', { name: 'Run cleanup' }) as HTMLButtonElement
      await vi.waitFor(() => expect(answer).toBeDefined())
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
      expect(run.disabled).toBe(true)
      expect(document.getElementById(run.getAttribute('aria-describedby')!.split(' ').at(-1)!)?.textContent).toBe('Checking the colour to remove…')
      await act(async () => answer!())
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      expect(document.getElementById('cleanup-remove-reason')).toBeNull()
    })

    it('never saves the inherited removed colour into the draft, only a choice of the user\'s', async () => {
      // The server named the colour by its current name (a shared hex): a blur saves the
      // draft without it, so it is resolved afresh next time rather than frozen.
      const duplicate: Colour[] = [{ ...swapped[0], hex: '#E15F2D', rgb: [225, 95, 45] }, swapped[1]]
      const panel = await openCleanup(duplicate, { value: 'Orange', unresolved: false, hex: '#E15F2D', problem: null })
      const colour = within(panel).getByLabelText(/Remove colour/) as HTMLInputElement
      await vi.waitFor(() => expect(colour.value).toBe('Orange'))
      const width = within(panel).getByLabelText(/Width \(mm\)/) as HTMLInputElement
      fireEvent.focus(width); fireEvent.change(width, { target: { value: '90' } }); fireEvent.blur(width)
      await vi.waitFor(() => expect(draftOf().cleanup?.width_mm).toBe(90))
      expect(draftOf().cleanup).not.toHaveProperty('remove_colour')
      fireEvent.change(colour, { target: { value: 'Sand' } }); fireEvent.blur(colour)
      expect(draftOf().cleanup.remove_colour).toBe('Sand')
    })

    it('drops a draft holding the recorded name, which now names the other colour, but keeps a chosen one', async () => {
      localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ cleanup: { remove_colour: 'Sand', width_mm: 120 } }))
      const panel = await openCleanup(swapped)
      const colour = within(panel).getByLabelText(/Remove colour/) as HTMLInputElement
      await vi.waitFor(() => expect(colour.value).toBe('#E15F2D'))
      expect(draftOf().cleanup).toEqual({ width_mm: 120 })
      expect((within(panel).getByLabelText(/Width \(mm\)/) as HTMLInputElement).value).toBe('120')
      await runsWith(panel, '#E15F2D')
      cleanup(); mocks.startPipeline.mockClear()
      localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ cleanup: { remove_colour: 'Orange' } }))
      const chosen = await openCleanup(swapped)
      const typed = within(chosen).getByLabelText(/Remove colour/) as HTMLInputElement
      await vi.waitFor(() => expect(chosen.textContent).toContain('Removes Orange (#E15F2D)'))
      expect(typed.value).toBe('Orange')
      expect(draftOf().cleanup).toEqual({ remove_colour: 'Orange' })
    })
  })

  it('saves a pending cleanup draft when a field loses focus', async () => {
    serve([paletteRevision, revision])
    window.history.replaceState(null, '', `/#stage=cleanup&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Cleanup' })
    const colour = within(panel).getByLabelText(/Remove colour/) as HTMLInputElement
    fireEvent.change(colour, { target: { value: 'Sand' } })
    expect(localStorage.getItem(`vinylgen.project-settings.${project.id}`)).toBeNull()
    fireEvent.blur(colour)
    expect(JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) || '{}').cleanup.remove_colour).toBe('Sand')
  })

  it('shows the cleaned raster as Cleaned and the raster before cleanup as Flattened', async () => {
    serve([cleanupRevision, paletteRevision, revision])
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=cleanup-1`)
    render(createElement((await import('./App')).default))
    const group = (await screen.findAllByRole('group', { name: 'Artwork view' }))[0]
    expect(within(group).getByRole('button', { name: 'Cleaned' })).toBeTruthy()
    fireEvent.click(within(group).getByRole('button', { name: 'Cleaned' }))
    await vi.waitFor(() => expect(workspaceProps.current?.preferredLayerId).toBe('flattened-cleaned'))
    fireEvent.click(within(group).getByRole('button', { name: 'Flattened' }))
    await vi.waitFor(() => expect(workspaceProps.current?.preferredLayerId).toBe('flattened-id'))
    expect(workspaceProps.current?.layers?.find(layer => layer.id === 'flattened-id')?.name).toMatch(/before cleanup/)
    expect(mocks.effectiveArtifacts).toHaveBeenCalledWith('cleanup-1', expect.anything(), { beforeCleanup: true })
  })

  it('shows the trace size read-only from the active cleanup and sends no cleanup fields with a trace', async () => {
    const traceRun: Revision = { ...revision, id: 'trace-1', parent_revision_id: 'cleanup-1', kind: 'trace', created_at: '2026-01-03', settings: { trace: { overlap: 1 }, stale_concerns: ['export'] } }
    serve([traceRun, cleanupRevision, paletteRevision, revision])
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-1`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Trace' })
    expect(await within(panel).findByText('Size: 300 × 200 mm (set in Cleanup)')).toBeTruthy()
    expect(within(panel).getByText('Layer overlap (mm)')).toBeTruthy()
    expect(within(panel).queryByLabelText(/Width \(mm\)/)).toBeNull()
    const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    const sent = mocks.startPipeline.mock.calls[0][3].trace
    for (const key of ['width_mm', 'height_mm', 'remove_colour', 'cleanup', 'cleanup_action', 'min_detail_width', 'min_region_area', 'remove_below_width'])
      expect(sent).not.toHaveProperty(key)
  })

  it('blocks tracing once a flatten after the cleanup marks it stale, offering a one-click re-run', async () => {
    const reflattened: Revision = { ...paletteRevision, id: 'palette-2', parent_revision_id: 'cleanup-1', created_at: '2026-01-04' }
    serve([reflattened, cleanupRevision, paletteRevision, revision])
    let update: ((job: Job) => void) | undefined
    mocks.followJob.mockImplementation((_id: string, onJob: (job: Job) => void) => { update = onJob; return () => undefined })
    mocks.startPipeline.mockResolvedValue({ ...queuedJob, id: 'job-rerun', kind: 'cleanup' })
    // A pending Cleanup form draft that the re-run must neither use nor spend.
    const draft = { width_mm: 999, remove_colour: 'Sand', cleanup: false }
    localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ cleanup: draft }))
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=palette-2`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Trace' })
    expect(await within(panel).findByText('The artwork changed after Cleanup: re-run Cleanup')).toBeTruthy()
    const trace = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
    const rerun = within(panel).getByRole('button', { name: 'Re-run Cleanup with its last settings' }) as HTMLButtonElement
    expect(rerun.title).toMatch(/settings of the last Cleanup run, not the Cleanup form/)
    await vi.waitFor(() => expect(rerun.disabled).toBe(false))
    expect(trace.disabled).toBe(true)
    const group = (await screen.findAllByRole('group', { name: 'Artwork view' }))[0]
    expect(within(group).queryByRole('button', { name: 'Cleaned' })).toBeNull()
    // The re-run asks the server for the stale cleanup's recorded settings on the current
    // revision (it names the removed colour by hex); it sends no settings of its own.
    fireEvent.click(rerun)
    expect(mocks.startPipeline).toHaveBeenCalledWith('cleanup', project.id, 'palette-2', { rerun_last_cleanup: true })
    await vi.waitFor(() => expect(update).toBeDefined())
    await act(async () => update?.({ ...queuedJob, id: 'job-rerun', kind: 'cleanup', status: 'succeeded', progress: 1, result_revision_id: 'palette-2' }))
    expect(JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) || '{}').cleanup).toEqual(draft)
  })

  it('edits the raster from before the cleanup, never the Cleaned image', async () => {
    // Record which raster the editor decodes into its pixel buffer.
    const decoded: string[] = []
    const descriptor = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src')!
    vi.spyOn(HTMLImageElement.prototype, 'src', 'set').mockImplementation(function (this: HTMLImageElement, value: string) { decoded.push(value); descriptor.set!.call(this, value) })
    serve([cleanupRevision, paletteRevision, revision])
    window.history.replaceState(null, '', `/#stage=edit&project=${project.id}&revision=cleanup-1`)
    render(createElement((await import('./App')).default))
    const group = (await screen.findAllByRole('group', { name: 'Artwork view' }))[0]
    expect(within(group).getByRole('button', { name: 'Cleaned' })).toBeTruthy()
    await vi.waitFor(() => expect(pressedView('Flattened')).toBe('true'))
    await vi.waitFor(() => expect(workspaceProps.current?.preferredLayerId).toBe('flattened-id'))
    await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeInstanceOf(Function))
    // On the Cleaned image, editing is disabled and points back to the editable one.
    fireEvent.click(within(group).getByRole('button', { name: 'Cleaned' }))
    await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeUndefined())
    expect(screen.getByText(/Switch to the Flattened image to select and edit areas\./)).toBeTruthy()
    expect(decoded.some(url => url.endsWith('/artifacts/flattened'))).toBe(true)
    expect(decoded.some(url => url.endsWith('/artifacts/flattened-cleaned'))).toBe(false)
    vi.restoreAllMocks()
  })

  it('leaves tracing allowed in pixels when no cleanup was ever run', async () => {
    serve([paletteRevision, revision])
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Trace' })
    expect(await within(panel).findByText('No physical size (px)')).toBeTruthy()
    const trace = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
    await vi.waitFor(() => expect(trace.disabled).toBe(false))
    expect(within(panel).queryByText(/re-run Cleanup/)).toBeNull()
  })

  it('offers no Cleaned view for a cleanup run that changed no pixels', async () => {
    const off: Revision = { ...cleanupRevision, id: 'cleanup-off', settings: { cleanup: { width_mm: 300, cleanup: false }, stale_concerns: ['trace', 'validation', 'export'] } }
    serve([off, paletteRevision, revision])
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=cleanup-off`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Trace' })
    // Its size still governs the trace.
    expect(await within(panel).findByText('Size: 300 × 200 mm (set in Cleanup)')).toBeTruthy()
    const group = (await screen.findAllByRole('group', { name: 'Artwork view' }))[0]
    expect(within(group).getByRole('button', { name: 'Flattened' })).toBeTruthy()
    expect(within(group).queryByRole('button', { name: 'Cleaned' })).toBeNull()
  })

  it('moves a pre-cleanup-stage trace draft\'s size and cleanup into the Cleanup draft', async () => {
    serve([paletteRevision, revision])
    localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { ...DEFAULT_TRACE_SETTINGS, corner_threshold: 45, width_mm: 250, height_mm: null, remove_colour: 'Sand', cleanup: true, cleanup_action: 'widen', min_detail_width: .7, min_region_area: 0, remove_below_width: 0 } }))
    window.history.replaceState(null, '', `/#stage=cleanup&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Cleanup' })
    const width = within(panel).getByLabelText(/Width \(mm\)/) as HTMLInputElement
    await vi.waitFor(() => expect(width.value).toBe('250'))
    expect((within(panel).getByLabelText(/Remove colour/) as HTMLInputElement).value).toBe('Sand')
    expect(width.closest('label')?.querySelector('.field-dirty')).toBeTruthy()
    const stored = JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) || '{}')
    expect(stored.cleanup).toMatchObject({ width_mm: 250, remove_colour: 'Sand', cleanup: true, cleanup_action: 'widen', min_detail_width: .7 })
    expect(stored.trace).toEqual({ ...DEFAULT_TRACE_SETTINGS, corner_threshold: 45 })
  })

  it('loads a pre-cleanup-stage trace run\'s size and cleanup into the Cleanup form, not the Trace form', async () => {
    const legacyRun: Revision = { ...revision, id: 'trace-1', parent_revision_id: 'palette-1', kind: 'trace', created_at: '2026-01-02',
      settings: { trace: { overlap: 2, width_mm: 400, height_mm: null, remove_colour: 'Sand', cleanup: true, cleanup_action: 'widen', min_detail_width: .5, min_region_area: 0, remove_below_width: 0 } } }
    serve([legacyRun, paletteRevision, revision])
    window.history.replaceState(null, '', `/#stage=cleanup&project=${project.id}&revision=trace-1`)
    render(createElement((await import('./App')).default))
    const cleanupPanel = screen.getByRole('tabpanel', { name: 'Cleanup' })
    const width = within(cleanupPanel).getByLabelText(/Width \(mm\)/) as HTMLInputElement
    await vi.waitFor(() => expect(width.value).toBe('400'))
    expect((within(cleanupPanel).getByLabelText(/Remove colour/) as HTMLInputElement).value).toBe('Sand')
    expect((within(cleanupPanel).getByLabelText(/Cleanup action/) as HTMLSelectElement).value).toBe('widen')
    expect((within(cleanupPanel).getByRole('checkbox', { name: /Enable cutter cleanup/ }) as HTMLInputElement).checked).toBe(true)
    // Recorded values, not pending edits.
    expect(width.closest('label')?.querySelector('.field-dirty')).toBeNull()
    // The legacy trace run only supplies the form's baseline; it is not a Cleanup
    // iteration, and no cleanup has ever actually run.
    expect(within(cleanupPanel).getByText('No past runs')).toBeTruthy()
    // The run's own overlap stays on Trace, and a new trace sends none of the moved fields.
    const tracePanel = screen.getByRole('tabpanel', { name: 'Trace' })
    expect((within(tracePanel).getByLabelText(/Layer overlap/) as HTMLInputElement).value).toBe('2')
    const run = within(tracePanel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    const sent = mocks.startPipeline.mock.calls[0][3].trace
    expect(sent.overlap).toBe(2)
    expect(sent).not.toHaveProperty('width_mm')
    expect(sent).not.toHaveProperty('cleanup')
  })

  it('steps the Cleanup form through prior cleanup runs without changing the shown revision', async () => {
    const newer: Revision = { ...cleanupRevision, id: 'cleanup-2', parent_revision_id: 'cleanup-1', created_at: '2026-01-03',
      settings: { cleanup: { width_mm: 500 }, stale_concerns: ['trace', 'validation', 'export'] } }
    serve([newer, cleanupRevision, paletteRevision, revision])
    window.history.replaceState(null, '', `/#stage=cleanup&project=${project.id}&revision=cleanup-2`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Cleanup' })
    const width = within(panel).getByLabelText(/Width \(mm\)/) as HTMLInputElement
    await vi.waitFor(() => expect(width.value).toBe('500'))
    fireEvent.click(within(panel).getByLabelText("Load an older iteration's settings"))
    expect(width.value).toBe('300')
    expect(within(panel).getByText('Iteration 1 of 2')).toBeTruthy()
    expect(window.location.hash).toContain('revision=cleanup-2')
    fireEvent.click(within(panel).getByLabelText("Load a newer iteration's settings"))
    expect(width.value).toBe('500')
  })
})

describe('legacy curve optimisation settings', () => {
  const paletteRevision: Revision = { ...revision, id: 'palette-1', kind: 'palette', settings: { palette: {} } }
  const legacyRun: Revision = { ...revision, id: 'trace-1', parent_revision_id: 'palette-1', kind: 'trace', created_at: '2026-01-02',
    settings: { trace: { curve_optimization: false, straight_line_max_deviation: 1.6, overlap: 0 } } }
  const currentRun: Revision = { ...legacyRun, settings: { trace: { curve_optimization: true, straight_line_max_deviation: 1.2 } } }

  it('maps a recorded curve_optimization false to a straight-line deviation of 0 and drops the flag', () => {
    expect(inheritedPipelineSettings([legacyRun, paletteRevision], 'trace-1').trace).toEqual({ straight_line_max_deviation: 0, corner_straightness: 1.6, overlap: 0 })
    expect(inheritedPipelineSettings([currentRun, paletteRevision], 'trace-1').trace).toEqual({ straight_line_max_deviation: 1.2, corner_straightness: 1.2 })
  })

  async function openTrace(runs: Revision[]) {
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [...runs, paletteRevision, revision] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened'), artifact('segmentation', 'application/octet-stream')])
    mocks.editor.mockResolvedValue({ width: 4344, height: 1448, protected_pixels: 0, palette: [] })
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=${runs[0].id}`)
    render(createElement((await import('./App')).default))
    return screen.getByRole('tabpanel', { name: 'Trace' })
  }

  it('loads a legacy revision as deviation 0, not pending, and never sends the flag', async () => {
    const panel = await openTrace([legacyRun])
    const deviation = within(panel).getByLabelText(/Straight-line deviation/) as HTMLInputElement
    await vi.waitFor(() => expect(deviation.value).toBe('0'))
    expect(deviation.closest('label')?.querySelector('.field-dirty')).toBeNull()
    // Corners are still detected with the deviation the run recorded.
    const corners = within(panel).getByLabelText(/Corner straightness/) as HTMLInputElement
    expect(corners.value).toBe('1.6')
    expect(corners.closest('label')?.querySelector('.field-dirty')).toBeNull()
    expect(within(panel).queryByRole('checkbox', { name: /Optimize curves/ })).toBeNull()
    const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    const sent = mocks.startPipeline.mock.calls[0][3].trace
    expect(sent.straight_line_max_deviation).toBe(0)
    expect(sent.corner_straightness).toBe(1.6)
    expect(sent).not.toHaveProperty('curve_optimization')
  })

  it('maps a legacy localStorage draft and rewrites it without the flag', async () => {
    // persistDraft stores the whole form, so an older draft carries every field it
    // knew: the curve_optimization flag and no corner_straightness.
    const { corner_straightness: _absent, ...fields } = DEFAULT_TRACE_SETTINGS
    const legacyDraft = { ...fields, curve_optimization: false, straight_line_max_deviation: 1, corner_threshold: 45 }
    localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: legacyDraft }))
    const panel = await openTrace([currentRun])
    const deviation = within(panel).getByLabelText(/Straight-line deviation/) as HTMLInputElement
    const corners = within(panel).getByLabelText(/Corner straightness/) as HTMLInputElement
    const threshold = within(panel).getByLabelText(/Corner threshold/) as HTMLInputElement
    // Corners keep the deviation the draft recorded; the deviation itself becomes 0.
    await vi.waitFor(() => expect(corners.value).toBe('1'))
    expect(deviation.value).toBe('0')
    expect(threshold.value).toBe('45')
    fireEvent.blur(deviation)
    const stored = JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) || '{}').trace
    expect(stored).not.toHaveProperty('curve_optimization')
    expect(stored).toEqual({ ...DEFAULT_TRACE_SETTINGS, straight_line_max_deviation: 0, corner_straightness: 1, corner_threshold: 45 })
  })

  it('states the traced image size from the editor state', async () => {
    const panel = await openTrace([currentRun])
    expect(await within(panel).findByText('Traced image: 4344 × 1448 px')).toBeTruthy()
  })
})

describe('the before-cleanup raster across trace runs', () => {
  const paletteRevision: Revision = { ...revision, id: 'palette-1', parent_revision_id: 'revision-1', kind: 'palette', settings: { palette: {} } }
  const cleanupRevision: Revision = { ...revision, id: 'cleanup-1', parent_revision_id: 'palette-1', kind: 'cleanup', created_at: '2026-01-02', settings: { cleanup: { width_mm: 300 }, stale_concerns: ['trace', 'validation', 'export'] } }
  const run1: Revision = { ...revision, id: 'trace-1', parent_revision_id: 'cleanup-1', kind: 'trace', created_at: '2026-01-03', settings: { trace: { overlap: 0 } } }
  const run2: Revision = { ...revision, id: 'trace-2', parent_revision_id: 'trace-1', kind: 'trace', created_at: '2026-01-04', settings: { trace: { overlap: 0 } } }
  const cleaned = { ...artifact('flattened'), id: 'flattened-cleaned', revision_id: 'cleanup-1', download_url: '/artifacts/flattened-cleaned' }
  const uncleaned = { ...artifact('flattened'), revision_id: 'palette-1' }
  const pressedView = () => within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).getByRole('button', { name: 'Flattened' }).getAttribute('aria-pressed')

  it('keeps the Flattened view on the uncleaned raster, fetched once, while moving between runs below the same cleanup', async () => {
    // Every preferred layer the canvas is ever handed, not just the settled one.
    const preferred: (string | undefined)[] = []
    let captured: CapturedWorkspace | null = null
    const shown = () => captured as CapturedWorkspace | null
    Object.defineProperty(workspaceProps, 'current', { configurable: true, get: () => captured, set: value => { captured = value; if (value) preferred.push(value.preferredLayerId) } })
    try {
      const pendingRuns: Record<string, () => void> = {}
      mocks.projects.mockResolvedValue([project])
      mocks.project.mockResolvedValue({ ...project, revisions: [run2, run1, cleanupRevision, paletteRevision, revision] })
      mocks.effectiveArtifacts.mockImplementation(async (id: string, _signal?: AbortSignal, options?: { beforeCleanup?: boolean }) => {
        if (options?.beforeCleanup) return [artifact('source'), uncleaned]
        const rows = [artifact('source'), cleaned, artifact('segmentation', 'application/octet-stream'),
          { ...artifact('traced-edge', 'image/svg+xml'), id: `${id}-edge`, revision_id: id }]
        if (id === 'trace-1') await new Promise<void>(resolve => { pendingRuns[id] = resolve })
        return rows
      })
      window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-2`)
      render(createElement((await import('./App')).default))
      const panel = screen.getByRole('tabpanel', { name: 'Trace' })
      await within(panel).findByText('Iteration 2 of 2')
      await screen.findAllByRole('group', { name: 'Artwork view' })
      fireEvent.click(within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).getByRole('button', { name: 'Flattened' }))
      await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('flattened-id'))
      await vi.waitFor(() => expect(shown()?.loading).toBe(false))
      const lookups = () => mocks.effectiveArtifacts.mock.calls.filter(call => call[2]?.beforeCleanup).length
      expect(lookups()).toBe(1)
      preferred.length = 0

      // Navigating to the older run (stepping would also switch to the Traced view).
      act(() => {
        window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-1`)
        window.dispatchEvent(new HashChangeEvent('hashchange'))
      })
      await vi.waitFor(() => expect(pendingRuns['trace-1']).toBeDefined())
      expect(shown()?.loading).toBe(true)
      await act(async () => pendingRuns['trace-1']())
      await within(panel).findByText('Iteration 1 of 2')
      await vi.waitFor(() => expect(shown()?.loading).toBe(false))
      // The same cleanup's raster needs no second lookup, and the Flattened view never
      // showed (or fetched) the cleaned raster in between.
      expect(lookups()).toBe(1)
      expect(preferred.length).toBeGreaterThan(0)
      expect(pressedView()).toBe('true')
      expect(preferred).toEqual(preferred.map(() => 'flattened-id'))
      expect(shown()?.layers?.some(layer => layer.id === 'flattened-id')).toBe(true)
    } finally {
      Object.defineProperty(workspaceProps, 'current', { configurable: true, writable: true, value: null })
    }
  })
})

describe('shown artwork stays consistent across cleanups', () => {
  const stale = ['cleanup', 'trace', 'validation', 'export']
  const paletteRevision: Revision = { ...revision, id: 'palette-1', parent_revision_id: 'revision-1', kind: 'palette', settings: { palette: {}, stale_concerns: stale } }
  const cleanup1: Revision = { ...revision, id: 'cleanup-1', parent_revision_id: 'palette-1', kind: 'cleanup', created_at: '2026-01-02', settings: { cleanup: { width_mm: 300 }, stale_concerns: ['trace', 'validation', 'export'] } }
  const trace1: Revision = { ...revision, id: 'trace-1', parent_revision_id: 'cleanup-1', kind: 'trace', created_at: '2026-01-03', settings: { trace: { overlap: 0 } } }
  // A re-flatten after the first cleanup, then a second cleanup of that new raster.
  const palette2: Revision = { ...revision, id: 'palette-2', parent_revision_id: 'cleanup-1', kind: 'palette', created_at: '2026-01-04', settings: { palette: {}, stale_concerns: stale } }
  const cleanup2: Revision = { ...revision, id: 'cleanup-2', parent_revision_id: 'palette-2', kind: 'cleanup', created_at: '2026-01-05', settings: { cleanup: { width_mm: 300 }, stale_concerns: ['trace', 'validation', 'export'] } }
  // An area edit of the first cleanup's input, then a cleanup of the edited raster.
  const edit1: Revision = { ...revision, id: 'edit-1', parent_revision_id: 'cleanup-1', kind: 'label-edit', created_at: '2026-01-06', settings: { stale_concerns: stale } }
  const cleanup3: Revision = { ...revision, id: 'cleanup-3', parent_revision_id: 'edit-1', kind: 'cleanup', created_at: '2026-01-07', settings: { cleanup: { width_mm: 300 }, stale_concerns: ['trace', 'validation', 'export'] } }
  const raster = (id: string, owner: string) => ({ ...artifact('flattened'), id, revision_id: owner, download_url: `/artifacts/${id}` })
  const segmentation = () => artifact('segmentation', 'application/octet-stream')
  const main: Record<string, Artifact[]> = {
    'cleanup-1': [artifact('source'), raster('clean-1', 'cleanup-1'), segmentation()],
    'trace-1': [artifact('source'), raster('clean-1', 'cleanup-1'), segmentation()],
    'palette-2': [artifact('source'), raster('flat-Q', 'palette-2'), segmentation()],
    'cleanup-2': [artifact('source'), raster('clean-2', 'cleanup-2'), segmentation()],
    'edit-1': [artifact('source'), raster('flat-E', 'edit-1'), segmentation()],
    'cleanup-3': [artifact('source'), raster('clean-3', 'cleanup-3'), segmentation()],
  }
  const before: Record<string, Artifact[]> = {
    'cleanup-1': [artifact('source'), raster('flat-P', 'palette-1')],
    'cleanup-2': [artifact('source'), raster('flat-Q', 'palette-2')],
    'cleanup-3': [artifact('source'), raster('flat-E', 'edit-1')],
  }
  // A revision's own artifacts: the flatten's original raster.
  const own: Record<string, Artifact[]> = { 'palette-1': [artifact('source'), raster('flat-P', 'palette-1')] }
  // Requests to hold (`main:<id>`, `before:<id>` or `own:<id>`) until released, or to
  // fail. Like fetch, a request rejects once its signal aborts, except `late` ones,
  // whose response was already on its way.
  let hold: Set<string>, fail: Set<string>, late: Set<string>, release: Record<string, () => void>
  async function respond<T>(key: string, signal: AbortSignal | undefined, value: () => T): Promise<T> {
    const aborted = () => new DOMException('The operation was aborted.', 'AbortError')
    if (hold.has(key)) await new Promise<void>((resolve, reject) => {
      release[key] = resolve
      if (!late.has(key)) signal?.addEventListener('abort', () => reject(aborted()), { once: true })
    })
    if (signal?.aborted && !late.has(key)) throw aborted()
    if (fail.has(key)) { fail.delete(key); throw new Error('lookup failed') }
    return value()
  }
  const sizes: Record<string, [number, number]> = {
    'cleanup-1': [600, 400], 'trace-1': [600, 400], 'cleanup-2': [610, 410], 'palette-2': [640, 430], 'edit-1': [630, 430], 'cleanup-3': [620, 420],
  }
  const tracedSizeOf = () => (captured as { livePreview?: { tracedSize?: unknown } } | null)?.livePreview?.tracedSize
  const livePreview = () => (captured as { livePreview?: unknown } | null)?.livePreview
  const previewPaused = () => (captured as { livePreviewPaused?: boolean } | null)?.livePreviewPaused
  const pressed = (name: string) => within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).queryByRole('button', { name })?.getAttribute('aria-pressed') ?? null
  let history: { preferred?: string; layers: string[] }[], captured: CapturedWorkspace | null
  // Every live-preview state the canvas is handed: which revision, which grid, paused.
  let previews: { revisionId: string; tracedSize: { width: number; height: number } | null; paused?: boolean }[] = []
  const previewStatus = () => (captured as { livePreviewStatus?: string } | null)?.livePreviewStatus
  const shown = () => captured as CapturedWorkspace | null

  beforeEach(() => {
    hold = new Set(); fail = new Set(); late = new Set(); release = {}; history = []; previews = []; captured = null
    Object.defineProperty(workspaceProps, 'current', { configurable: true, get: () => captured, set: value => {
      captured = value
      if (value) history.push({ preferred: value.preferredLayerId, layers: (value.layers ?? []).map((layer: { id: string }) => layer.id) })
      if (value?.livePreview) previews.push({ revisionId: value.livePreview.revisionId, tracedSize: value.livePreview.tracedSize, paused: value.livePreviewPaused })
    } })
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [cleanup3, edit1, cleanup2, palette2, trace1, cleanup1, paletteRevision, revision] })
    // Each revision's own traced grid, so the size shown names the revision it came from.
    mocks.editor.mockImplementation((id: string, signal?: AbortSignal) =>
      respond(`editor:${id}`, signal, () => ({ width: sizes[id][0], height: sizes[id][1], protected_pixels: 0, palette: [] })))
    mocks.effectiveArtifacts.mockImplementation((id: string, signal?: AbortSignal, options?: { beforeCleanup?: boolean }) =>
      respond(`${options?.beforeCleanup ? 'before' : 'main'}:${id}`, signal, () => (options?.beforeCleanup ? before : main)[id]))
    mocks.artifacts.mockImplementation((id: string, signal?: AbortSignal) => respond(`own:${id}`, signal, () => own[id] ?? []))
  })
  afterEach(() => { Object.defineProperty(workspaceProps, 'current', { configurable: true, writable: true, value: null }) })

  async function openAt(revisionId: string, view?: string) {
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=${revisionId}`)
    render(createElement((await import('./App')).default))
    await screen.findAllByRole('group', { name: 'Artwork view' })
    if (view) {
      await vi.waitFor(() => expect(pressed(view)).not.toBeNull())
      fireEvent.click(within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).getByRole('button', { name: view }))
      await vi.waitFor(() => expect(pressed(view)).toBe('true'))
    }
  }
  function navigate(revisionId: string) {
    act(() => {
      window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=${revisionId}`)
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
  }

  it('keeps the previous artwork, view and no editable raster until a jump to another cleanup has loaded', async () => {
    await openAt('cleanup-1', 'Cleaned')
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('clean-1'))
    await vi.waitFor(() => expect(mocks.editor).toHaveBeenCalledWith('cleanup-1', expect.anything()))
    await screen.findByText('Traced image: 600 × 400 px')
    hold.add('main:cleanup-2')
    history = []
    navigate('cleanup-2')
    await vi.waitFor(() => expect(release['main:cleanup-2']).toBeDefined())
    // Mid-load: the old artwork as it was, still on the Cleaned view, and nothing to edit.
    expect(shown()?.loading).toBe(true)
    expect(pressed('Cleaned')).toBe('true')
    expect(history.every(entry => entry.preferred === 'clean-1' && entry.layers.includes('flat-P'))).toBe(true)
    expect(mocks.editor).not.toHaveBeenCalledWith('cleanup-2', expect.anything())
    // The new traced grid is unknown until the jump's artifacts load: the size line and
    // the Live preview switch stay put, and the preview pauses.
    expect(screen.getByText('Traced image: 600 × 400 px')).toBeTruthy()
    expect(livePreview()).toBeDefined()
    expect(previewPaused()).toBe(true)
    await act(async () => release['main:cleanup-2']())
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('clean-2'))
    await screen.findByText('Traced image: 610 × 410 px')
    expect(tracedSizeOf()).toEqual({ width: 610, height: 410 })
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    expect(pressed('Cleaned')).toBe('true')
    await vi.waitFor(() => expect(mocks.editor).toHaveBeenCalledWith('cleanup-2', expect.anything()))
  })

  it('never pairs a new cleanup with an older cleanup\'s before-cleanup raster', async () => {
    await openAt('cleanup-1', 'Flattened')
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('flat-P'))
    hold.add('before:cleanup-2')
    history = []
    navigate('cleanup-2')
    await vi.waitFor(() => expect(release['before:cleanup-2']).toBeDefined())
    expect(shown()?.loading).toBe(true)
    await act(async () => release['before:cleanup-2']())
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('flat-Q'))
    expect(pressed('Flattened')).toBe('true')
    // Until the new cleanup's own raster arrived, the canvas kept the old artwork whole.
    for (const entry of history) {
      if (entry.layers.includes('clean-2')) expect(entry.preferred).toBe('flat-Q')
      else expect(entry.preferred).toBe('flat-P')
      expect(entry.layers.includes('clean-2') && entry.layers.includes('flat-P')).toBe(false)
    }
  })

  it('reports a failed before-cleanup lookup, never shows the cleaned raster as Flattened, and retries', async () => {
    fail.add('before:cleanup-1')
    await openAt('cleanup-1', 'Cleaned')
    expect((await screen.findByRole('alert')).textContent).toContain('before the cleanup')
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('clean-1'))
    // The Flattened view has nothing true to show, so it is not offered; nothing is editable.
    expect(pressed('Flattened')).toBeNull()
    expect(pressed('Cleaned')).toBe('true')
    // The traced grid is the cleaned raster's, so the live preview keeps working.
    await screen.findByText('Traced image: 600 × 400 px')
    expect(livePreview()).toBeDefined()
    // The next revision change below the same cleanup looks it up again; its success
    // clears the error it had reported.
    navigate('trace-1')
    await vi.waitFor(() => expect(pressed('Flattened')).toBe('false'))
    expect(mocks.effectiveArtifacts.mock.calls.filter(call => call[0] === 'cleanup-1' && call[2]?.beforeCleanup)).toHaveLength(2)
    await vi.waitFor(() => expect(mocks.editor).toHaveBeenCalledWith('trace-1', expect.anything()))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('commits an edited cleanup with its own pre-edit original and before-cleanup raster, never an older one', async () => {
    await openAt('cleanup-1', 'Flattened')
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('flat-P'))
    hold.add('before:cleanup-3'); hold.add('own:palette-1')
    history = []
    navigate('cleanup-3')
    // The pre-edit original loads alongside the artifacts, before-cleanup after them.
    await vi.waitFor(() => expect(release['own:palette-1']).toBeDefined())
    expect(shown()?.loading).toBe(true)
    await act(async () => release['own:palette-1']())
    await vi.waitFor(() => expect(release['before:cleanup-3']).toBeDefined())
    // Nothing of the new cleanup is shown while either of its rasters is outstanding.
    expect(history.every(entry => !entry.layers.includes('clean-3') && entry.preferred === 'flat-P')).toBe(true)
    await act(async () => release['before:cleanup-3']())
    await vi.waitFor(() => expect(shown()?.layers?.some(layer => layer.id === 'clean-3')).toBe(true))
    // Flattened is the pre-edit original; Edited is the edited raster the cleanup started from.
    expect(pressed('Flattened')).toBe('true')
    expect(shown()?.preferredLayerId).toBe('flat-P')
    expect(shown()?.layers?.find(layer => layer.id === 'flat-P')?.name).toContain('flattened (original)')
    for (const entry of history.filter(entry => entry.layers.includes('clean-3'))) {
      expect(entry.layers).toEqual(expect.arrayContaining(['flat-P', 'flat-E']))
      expect(entry.preferred).toBe('flat-P')
    }
    fireEvent.click(within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).getByRole('button', { name: 'Edited' }))
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('flat-E'))
    // The palette's original is reused, not looked up again, for the same flatten.
    navigate('cleanup-1'); navigate('cleanup-3')
    await vi.waitFor(() => expect(shown()?.layers?.some(layer => layer.id === 'clean-3')).toBe(true))
    expect(mocks.artifacts.mock.calls.filter(call => call[0] === 'palette-1')).toHaveLength(1)
  })

  it('reports a failed pre-edit lookup, never shows another raster as Flattened, and retries', async () => {
    fail.add('own:palette-1')
    await openAt('edit-1', 'Edited')
    expect((await screen.findByRole('alert')).textContent).toContain('before the edits')
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('flat-E'))
    // The edited raster is not passed off as the Flattened original.
    expect(pressed('Flattened')).toBeNull()
    expect(history.some(entry => entry.preferred === undefined && entry.layers.includes('flat-E'))).toBe(false)
    // The next revision change with the same flatten looks it up again.
    navigate('cleanup-3')
    await vi.waitFor(() => expect(pressed('Flattened')).toBe('false'))
    expect(mocks.artifacts.mock.calls.filter(call => call[0] === 'palette-1')).toHaveLength(2)
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.click(within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).getByRole('button', { name: 'Flattened' }))
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('flat-P'))
  })

  it('clears only the error its own lookup reported', async () => {
    fail.add('before:cleanup-1')
    await openAt('cleanup-1', 'Cleaned')
    await screen.findByRole('alert')
    // While the retry is in flight, an unrelated failure replaces the message.
    hold.add('before:cleanup-1')
    navigate('trace-1')
    await vi.waitFor(() => expect(release['before:cleanup-1']).toBeDefined())
    mocks.deleteProject.mockRejectedValueOnce(new Error('delete refused'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete project' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Delete project' }))
    await vi.waitFor(() => expect(screen.getByRole('alert').textContent).toContain('delete refused'))
    // The lookup's success leaves that other error alone.
    await act(async () => release['before:cleanup-1']())
    await vi.waitFor(() => expect(pressed('Flattened')).toBe('false'))
    expect(screen.getByRole('alert').textContent).toContain('delete refused')
  })

  it('never commits a load that a later navigation superseded, even one settling just before its commit', async () => {
    await openAt('cleanup-1', 'Cleaned')
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('clean-1'))
    // A held artifacts request is aborted by the next navigation.
    hold.add('main:cleanup-2')
    navigate('cleanup-2')
    await vi.waitFor(() => expect(release['main:cleanup-2']).toBeDefined())
    navigate('trace-1')
    await vi.waitFor(() => expect(shown()?.loading).toBe(false))
    await act(async () => release['main:cleanup-2']())
    // A pre-edit lookup whose response arrives after its load was superseded.
    hold.add('own:palette-1'); late.add('own:palette-1')
    navigate('edit-1')
    await vi.waitFor(() => expect(release['own:palette-1']).toBeDefined())
    navigate('cleanup-1')
    await vi.waitFor(() => expect(shown()?.loading).toBe(false))
    history = []
    await act(async () => release['own:palette-1']())
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect(window.location.hash).toContain('revision=cleanup-1')
    expect(shown()?.layers?.some(layer => layer.id === 'clean-1')).toBe(true)
    expect(shown()?.layers?.some(layer => layer.id === 'flat-E' || layer.id === 'clean-2')).toBe(false)
    expect(history.every(entry => entry.layers.includes('clean-1'))).toBe(true)
  })

  it('clears a lookup\'s error when the next revision has nothing to look up, and never reports a superseded one', async () => {
    fail.add('before:cleanup-1')
    await openAt('cleanup-1', 'Cleaned')
    expect((await screen.findByRole('alert')).textContent).toContain('before the cleanup')
    // No cleanup behind the re-flatten: nothing to look up, and the old reason is gone.
    navigate('palette-2')
    await screen.findByText('Traced image: 640 × 430 px')
    expect(screen.queryByRole('alert')).toBeNull()
    // A pre-edit lookup fails, but its load is superseded before it commits.
    fail.add('own:palette-1'); hold.add('main:cleanup-3')
    navigate('cleanup-3')
    await vi.waitFor(() => expect(release['main:cleanup-3']).toBeDefined())
    await vi.waitFor(() => expect(mocks.artifacts.mock.calls.some(call => call[0] === 'palette-1')).toBe(true))
    // Its failure is only reported if its load commits, so nothing shows yet.
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect(screen.queryByRole('alert')).toBeNull()
    navigate('cleanup-1')
    await screen.findByText('Traced image: 600 × 400 px')
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('reports only a failed artifacts request, cancelling the pre-edit lookup running beside it', async () => {
    await openAt('cleanup-1', 'Cleaned')
    await screen.findByText('Traced image: 600 × 400 px')
    hold.add('own:palette-1'); fail.add('main:edit-1')
    navigate('edit-1')
    expect((await screen.findByRole('alert')).textContent).toContain('lookup failed')
    const lookup = mocks.artifacts.mock.calls.find(call => call[0] === 'palette-1')
    expect((lookup?.[1] as AbortSignal | undefined)?.aborted).toBe(true)
    await act(async () => release['own:palette-1']?.())
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect(screen.getByRole('alert').textContent).toContain('lookup failed')
    expect(screen.getByRole('alert').textContent).not.toContain('before the edits')
  })

  it('never runs the preview on another revision\'s traced grid across a jump', async () => {
    await openAt('cleanup-1', 'Cleaned')
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    hold.add('main:cleanup-2')
    navigate('cleanup-2')
    await vi.waitFor(() => expect(release['main:cleanup-2']).toBeDefined())
    await act(async () => release['main:cleanup-2']())
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    expect(previews.some(state => state.revisionId === 'cleanup-2' && state.paused)).toBe(true)
    // Unpaused, the preview always carries its own revision's grid.
    for (const state of previews.filter(state => !state.paused)) {
      expect(state.tracedSize).toEqual({ width: sizes[state.revisionId][0], height: sizes[state.revisionId][1] })
    }
  })

  it('leaves loading when a revision\'s artifacts fail, keeping the old artwork, and retries on the next change', async () => {
    await openAt('cleanup-1', 'Cleaned')
    await vi.waitFor(() => expect(shown()?.loading).toBe(false))
    fail.add('main:cleanup-2')
    navigate('cleanup-2')
    expect((await screen.findByRole('alert')).textContent).toContain('lookup failed')
    await vi.waitFor(() => expect(shown()?.loading).toBe(false))
    expect(shown()?.preferredLayerId).toBe('clean-1')
    expect(previewPaused()).toBe(true)
    expect(previewStatus()).toBe('Preview unavailable: the selected revision failed to load.')
    // The next revision change loads normally again.
    navigate('trace-1')
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    expect(previewStatus()).toBeUndefined()
    navigate('cleanup-2')
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('clean-2'))
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    expect(mocks.effectiveArtifacts.mock.calls.filter(call => call[0] === 'cleanup-2' && !call[2]?.beforeCleanup)).toHaveLength(2)
  })

  it('says why the preview is paused when the traced grid cannot be measured, and retries', async () => {
    await openAt('cleanup-1', 'Cleaned')
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    fail.add('editor:cleanup-2')
    navigate('cleanup-2')
    await vi.waitFor(() => expect(previewStatus()).toBe('Preview unavailable: Error: lookup failed'))
    expect(previewPaused()).toBe(true)
    expect(shown()?.loading).toBe(false)
    navigate('trace-1')
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    navigate('cleanup-2')
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    expect(previewStatus()).toBeUndefined()
    await screen.findByText('Traced image: 610 × 410 px')
  })

  it('keeps the Live preview switch, paused, on a revision with no flattened image and through the jump away', async () => {
    main['revision-1'] = [artifact('source')]
    try {
      window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=revision-1`)
      render(createElement((await import('./App')).default))
      await vi.waitFor(() => expect(livePreview()).toBeDefined())
      expect(tracedSizeOf()).toBeNull()
      expect(previewPaused()).toBe(true)
      hold.add('main:cleanup-1')
      previews = []; history = []
      navigate('cleanup-1')
      await vi.waitFor(() => expect(release['main:cleanup-1']).toBeDefined())
      await act(async () => release['main:cleanup-1']())
      await vi.waitFor(() => expect(previewPaused()).toBe(false))
      // Handed to the canvas on every render of the jump: the switch never unmounts.
      expect(history.length).toBeGreaterThan(0)
      expect(previews).toHaveLength(history.length)
      expect(tracedSizeOf()).toEqual({ width: 600, height: 400 })
    } finally { delete main['revision-1'] }
  })

  it('clears the old reason while the traced grid is measured again', async () => {
    await openAt('cleanup-1', 'Cleaned')
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    fail.add('editor:cleanup-2')
    navigate('cleanup-2')
    await vi.waitFor(() => expect(previewStatus()).toBe('Preview unavailable: Error: lookup failed'))
    navigate('trace-1')
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    hold.add('editor:cleanup-2')
    navigate('cleanup-2')
    await vi.waitFor(() => expect(release['editor:cleanup-2']).toBeDefined())
    // Measuring: paused, with no stale failure standing.
    expect(previewPaused()).toBe(true)
    expect(previewStatus()).toBeUndefined()
    await act(async () => release['editor:cleanup-2']())
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
  })

  // Every control that would act on the selected revision, with the reason shown.
  // Each locked job button is described by the reason, shown only once the load failed.
  function expectLockedButton(panelName: string, buttonName: string, reason: string, shown: boolean) {
    const button = within(screen.getByRole('tabpanel', { name: panelName })).getByRole('button', { name: buttonName }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    const described = document.getElementById(button.getAttribute('aria-describedby') ?? '')
    expect(described?.textContent).toContain(reason)
    expect(described?.classList.contains('visually-hidden')).toBe(!shown)
  }
  async function expectSelectedRevisionLocked(reason: string) {
    const failed = reason === 'The selected revision failed to load.'
    expectLockedButton('Trace', 'Trace and validate', reason, failed)
    expectLockedButton('Cleanup', 'Run cleanup', reason, failed)
    expectLockedButton('Flatten', 'Flatten image', reason, failed)
    // The palette editor (mocked here) is disabled with the reason.
    const palette = editorProps.current as unknown as { disabled?: boolean; disabledHint?: string }
    expect(palette.disabled).toBe(true)
    expect(palette.disabledHint).toBe(reason)
  }
  async function expectSelectedRevisionUnlocked() {
    const trace = screen.getByRole('tabpanel', { name: 'Trace' })
    await vi.waitFor(() => expect((within(trace).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement).disabled).toBe(false))
    expect((within(screen.getByRole('tabpanel', { name: 'Cleanup' })).getByRole('button', { name: 'Run cleanup' }) as HTMLButtonElement).disabled).toBe(false)
    expect((editorProps.current as unknown as { disabled?: boolean }).disabled).toBe(false)
  }

  it('locks every edit and job while the selected revision is loading, or failed to load', async () => {
    window.history.replaceState(null, '', `/#stage=palette&project=${project.id}&revision=cleanup-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(editorProps.current).not.toBeNull())
    await expectSelectedRevisionUnlocked()
    // Pending: R's artwork shown, R2 selected.
    hold.add('main:cleanup-2')
    navigate('cleanup-2')
    await vi.waitFor(() => expect(release['main:cleanup-2']).toBeDefined())
    await expectSelectedRevisionLocked('Loading the selected revision…')
    await act(async () => release['main:cleanup-2']())
    await expectSelectedRevisionUnlocked()
    // Failed: R2's artwork stays shown and R3 stays locked until it loads.
    fail.add('main:trace-1')
    navigate('trace-1')
    await screen.findByRole('alert')
    await expectSelectedRevisionLocked('The selected revision failed to load.')
    // The Edit panel's area editor too.
    fireEvent.click(screen.getByRole('tab', { name: 'Edit' }))
    await vi.waitFor(() => expect(screen.getByRole('tabpanel', { name: 'Edit' }).textContent).toContain('The selected revision failed to load.'))
    navigate('cleanup-1')
    await expectSelectedRevisionUnlocked()
  })

  it('retries a failed revision from its lock reason', async () => {
    await openAt('cleanup-1', 'Cleaned')
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    fail.add('main:cleanup-2')
    navigate('cleanup-2')
    await screen.findByRole('alert')
    const trace = screen.getByRole('tabpanel', { name: 'Trace' })
    await vi.waitFor(() => expect(trace.querySelector('.lock-reason')).not.toBeNull())
    fireEvent.click(within(trace.querySelector<HTMLElement>('.lock-reason')!).getByRole('button', { name: 'Retry' }))
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('clean-2'))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(trace.querySelector('.lock-reason')).toBeNull()
    expect(mocks.effectiveArtifacts.mock.calls.filter(call => call[0] === 'cleanup-2' && !call[2]?.beforeCleanup)).toHaveLength(2)
  })

  it('offers Retry loading only under the load\'s own error, and never clears another one', async () => {
    await openAt('cleanup-1', 'Cleaned')
    await vi.waitFor(() => expect(previewPaused()).toBe(false))
    fail.add('main:cleanup-2')
    navigate('cleanup-2')
    await screen.findByRole('button', { name: 'Retry loading' })
    // An unrelated failure replaces the message: no load retry is offered under it.
    mocks.deleteProject.mockRejectedValueOnce(new Error('delete refused'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete project' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Delete project' }))
    await vi.waitFor(() => expect(screen.getByRole('alert').textContent).toContain('delete refused'))
    expect(screen.queryByRole('button', { name: 'Retry loading' })).toBeNull()
    // The lock reason's Retry reloads the revision and leaves the delete error standing.
    const trace = screen.getByRole('tabpanel', { name: 'Trace' })
    fireEvent.click(within(trace.querySelector<HTMLElement>('.lock-reason')!).getByRole('button', { name: 'Retry' }))
    await vi.waitFor(() => expect(shown()?.preferredLayerId).toBe('clean-2'))
    expect(screen.getByRole('alert').textContent).toContain('delete refused')
  })

  it('retries a project that failed to load on a switch', async () => {
    mocks.project.mockRejectedValueOnce(new Error('server unavailable'))
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=cleanup-1`)
    render(createElement((await import('./App')).default))
    expect((await screen.findByRole('alert')).textContent).toContain('server unavailable')
    fireEvent.click(await screen.findByRole('button', { name: 'Retry loading' }))
    await vi.waitFor(() => expect(shown()?.layers?.some(layer => layer.id === 'clean-1')).toBe(true))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry loading' })).toBeNull()
  })
})

describe('editor pixels right after an area or pencil edit', () => {
  const paletteRevision: Revision = { ...revision, id: 'palette-1', parent_revision_id: 'revision-1', kind: 'palette', settings: { palette: {} } }
  const edited: Revision = { ...revision, id: 'edit-9', parent_revision_id: 'palette-1', kind: 'label-edit', created_at: '2026-01-02', settings: {} }
  const colour = (index: number, name: string, hex: string, rgb: [number, number, number]) => ({ index, name, hex, rgb, share_percent: 50 })
  // Each revision's own editor state: the edit renames its colours, so the swatches
  // show which revision's palette is in use.
  const editorStates: Record<string, { width: number; height: number; protected_pixels: number; palette: ReturnType<typeof colour>[] }> = {
    'palette-1': { width: 2, height: 1, protected_pixels: 0, palette: [colour(0, 'Navy', '#0F233C', [15, 35, 60]), colour(1, 'Orange', '#E15F2D', [225, 95, 45])] },
    'edit-9': { width: 2, height: 1, protected_pixels: 0, palette: [colour(0, 'Midnight', '#0F233C', [15, 35, 60]), colour(1, 'Ember', '#E15F2D', [225, 95, 45])] },
    'other-1': { width: 2, height: 1, protected_pixels: 0, palette: [colour(0, 'Moss', '#0F233C', [15, 35, 60]), colour(1, 'Sand', '#E15F2D', [225, 95, 45])] },
  }
  // A second project to switch to while an edit is in flight: its own flatten, orange|navy.
  const otherProject: Project = { ...project, id: 'project-2', slug: 'moon-forge', name: 'Moon forge' }
  const otherRevision: Revision = { ...revision, id: 'other-1', project_id: otherProject.id, kind: 'palette', settings: { palette: {} } }
  const NAVY = [15, 35, 60, 255], ORANGE = [225, 95, 45, 255]
  // Each 2 × 1 flattened PNG decodes to its own pixels: the flatten's navy|orange, the
  // area edit's orange|orange.
  const decoded: Record<string, number[]> = { '/artifacts/flat-palette-1': [...NAVY, ...ORANGE], '/artifacts/flat-edit-9': [...ORANGE, ...ORANGE], '/artifacts/flat-other-1': [...ORANGE, ...NAVY] }
  let decodedUrls: string[], releaseEdited: (() => void) | undefined
  class DecodedImage {
    onload: (() => void) | null = null
    naturalWidth = 2
    naturalHeight = 1
    url = ''
    set src(value: string) { this.url = value; decodedUrls.push(value); queueMicrotask(() => this.onload?.()) }
  }
  const workspace = () => workspaceProps.current as (CapturedWorkspace & { overlay?: { bounds: { width: number } }; onPaint?: (x: number, y: number) => void }) | null
  const panel = () => screen.getByRole('tabpanel', { name: 'Edit' })
  const swatch = (name: string) => panel().querySelector<HTMLButtonElement>(`button[aria-label*="${name}"]`)

  async function openEditor() {
    decodedUrls = []; releaseEdited = undefined
    vi.stubGlobal('Image', DecodedImage)
    let drawn: DecodedImage | null = null
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      drawImage: (image: DecodedImage) => { drawn = image },
      getImageData: () => ({ data: new Uint8ClampedArray(decoded[drawn!.url]) }),
    } as unknown as CanvasRenderingContext2D)
    const { api } = await import('./api')
    const applyEdits = vi.spyOn(api, 'applyEdits').mockResolvedValue(edited)
    mocks.projects.mockResolvedValue([project, otherProject])
    mocks.project.mockImplementation(async (id: string) => id === otherProject.id
      ? { ...otherProject, revisions: [otherRevision] } : { ...project, revisions: [edited, paletteRevision, revision] })
    mocks.editor.mockImplementation(async (id: string, signal?: AbortSignal) => {
      if (signal?.aborted) throw new DOMException('The operation was aborted.', 'AbortError')
      return editorStates[id]
    })
    // The edited revision's artifacts are held until the test releases them, once.
    let heldOnce = false
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => {
      if (id === 'edit-9' && !heldOnce) { heldOnce = true; await new Promise<void>(resolve => { releaseEdited = resolve }) }
      return [artifact('source'), { ...artifact('flattened'), id: `flat-${id}`, revision_id: id, download_url: `/artifacts/flat-${id}` }]
    })
    mocks.artifacts.mockResolvedValue([{ ...artifact('flattened'), id: 'flat-palette-1', revision_id: 'palette-1', download_url: '/artifacts/flat-palette-1' }])
    window.history.replaceState(null, '', `/#stage=edit&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(swatch('Orange')).not.toBeNull())
    await vi.waitFor(() => expect(decodedUrls).toContain('/artifacts/flat-palette-1'))
    return applyEdits
  }
  // Select the area at (x, y) and report the selection overlay's width: 2 when that
  // pixel's colour fills the whole raster, 1 when it is a single pixel.
  function selectedWidth(x: number, y: number) {
    act(() => workspace()!.onSelectPoint!([x, y]))
    const width = workspace()?.overlay?.bounds.width
    act(() => workspace()!.onSelectPoint!([x, y]))
    return width
  }
  function navigate(revisionId: string) {
    act(() => {
      window.history.replaceState(null, '', `/#stage=edit&project=${project.id}&revision=${revisionId}`)
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
  }

  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

  // Holds the edit request open, switches to the other project and lets it load fully,
  // then resolves the edit: the other project's revision, pixels, layers and editor
  // state must be exactly what its own load produced, never touched by the edit.
  async function resolveAfterSwitch(applyEdits: ReturnType<typeof vi.spyOn>, submit: () => void) {
    let resolveEdit: ((value: Revision) => void) | undefined
    applyEdits.mockImplementation(() => new Promise<Revision>(resolve => { resolveEdit = resolve }))
    submit()
    await vi.waitFor(() => expect(resolveEdit).toBeDefined())
    await chooseProject(otherProject)
    await vi.waitFor(() => expect(swatch('Moss')).not.toBeNull())
    await vi.waitFor(() => expect(decodedUrls).toContain('/artifacts/flat-other-1'))
    await vi.waitFor(() => expect(workspace()?.layers?.some(layer => layer.id === 'flat-other-1')).toBe(true))
    const replaceState = vi.spyOn(window.history, 'replaceState')
    await act(async () => { resolveEdit!(edited); await new Promise(resolve => setTimeout(resolve, 20)) })
    // Never even transiently on the edit's revision: no hash, load or editor request for it.
    expect(replaceState.mock.calls.some(call => String(call[2]).includes('edit-9'))).toBe(false)
    expect(window.location.hash).toContain(`project=${otherProject.id}`)
    expect(window.location.hash).toContain('revision=other-1')
    expect(mocks.effectiveArtifacts.mock.calls.some(call => call[0] === 'edit-9')).toBe(false)
    expect(mocks.editor.mock.calls.some(call => call[0] === 'edit-9')).toBe(false)
    // Its own palette, layers and pixels (orange|navy: the orange area is one pixel wide,
    // where the edit's orange|orange would fill the raster).
    expect(swatch('Moss')).not.toBeNull()
    expect(swatch('Ember')).toBeNull()
    expect(workspace()!.layers!.map(layer => layer.id)).toContain('flat-other-1')
    expect(workspace()!.layers!.some(layer => layer.id.includes('edit-9') || layer.id.includes('palette-1'))).toBe(false)
    fireEvent.click(within(panel()).getByRole('button', { name: 'Select areas' }))
    await vi.waitFor(() => expect(workspace()?.onSelectPoint).toBeDefined())
    expect(selectedWidth(0, 0)).toBe(1)
    expect(selectedWidth(1, 0)).toBe(1)
    expect(within(panel()).queryByRole('alert')).toBeNull()
  }

  it('drops an area edit that resolves after a project switch', async () => {
    const applyEdits = await openEditor()
    await vi.waitFor(() => expect(workspace()?.onSelectPoint).toBeDefined())
    await vi.waitFor(() => expect(selectedWidth(0, 0)).toBe(1))
    act(() => workspace()!.onSelectPoint!([0, 0]))
    await vi.waitFor(() => expect(swatch('Orange')!.disabled).toBe(false))
    await resolveAfterSwitch(applyEdits, () => fireEvent.click(swatch('Orange')!))
    expect(applyEdits).toHaveBeenCalledWith(project.id, 'palette-1', [{ tool: 'reassign_region', point: [0, 0], target_label: 1 }])
  })

  it('drops a pencil edit that resolves after a project switch', async () => {
    const applyEdits = await openEditor()
    fireEvent.click(within(panel()).getByRole('button', { name: 'Pencil' }))
    await vi.waitFor(() => expect(workspace()?.onPaint).toBeDefined())
    act(() => workspace()!.onPaint!(1, 0))
    await resolveAfterSwitch(applyEdits, () => fireEvent.click(within(panel()).getByRole('button', { name: 'Apply pencil edits' })))
    expect(applyEdits).toHaveBeenCalledWith(project.id, 'palette-1', [{ tool: 'paint_pixels', target_label: 0, pixels: [[1, 0]] }])
  })

  // Holds the edit open and returns its resolver. The first project then lists no edit
  // revision, so returning to it lands on its flatten with nothing held.
  function holdEdit(applyEdits: ReturnType<typeof vi.spyOn>) {
    let resolveEdit: ((value: Revision) => void) | undefined
    applyEdits.mockImplementation(() => new Promise<Revision>(resolve => { resolveEdit = resolve }))
    mocks.project.mockImplementation(async (id: string) => id === otherProject.id
      ? { ...otherProject, revisions: [otherRevision] } : { ...project, revisions: [paletteRevision, revision] })
    return () => resolveEdit
  }
  const areaBusy = () => within(panel()).queryByText(/Replacing colour/)

  it('shows an area edit as busy only on its own project, and clears it when it resolves elsewhere', async () => {
    const applyEdits = await openEditor()
    const resolver = holdEdit(applyEdits)
    await vi.waitFor(() => expect(workspace()?.onSelectPoint).toBeDefined())
    await vi.waitFor(() => expect(selectedWidth(0, 0)).toBe(1))
    act(() => workspace()!.onSelectPoint!([0, 0]))
    await vi.waitFor(() => expect(swatch('Orange')!.disabled).toBe(false))
    fireEvent.click(swatch('Orange')!)
    await vi.waitFor(() => expect(resolver()).toBeDefined())
    expect(areaBusy()).not.toBeNull()
    await chooseProject(otherProject)
    await vi.waitFor(() => expect(swatch('Moss')).not.toBeNull())
    expect(areaBusy()).toBeNull()
    // Back on its own project while it is still in flight, it shows busy again.
    await chooseProject(project)
    await vi.waitFor(() => expect(swatch('Orange')).not.toBeNull())
    expect(areaBusy()).not.toBeNull()
    await chooseProject(otherProject)
    await vi.waitFor(() => expect(swatch('Moss')).not.toBeNull())
    await act(async () => { resolver()!(edited); await new Promise(resolve => setTimeout(resolve, 20)) })
    await chooseProject(project)
    await vi.waitFor(() => expect(swatch('Orange')).not.toBeNull())
    expect(areaBusy()).toBeNull()
  })

  it('shows a pencil edit as busy only on its own project, and clears it when it resolves there', async () => {
    const applyEdits = await openEditor()
    const resolver = holdEdit(applyEdits)
    fireEvent.click(within(panel()).getByRole('button', { name: 'Pencil' }))
    await vi.waitFor(() => expect(workspace()?.onPaint).toBeDefined())
    act(() => workspace()!.onPaint!(1, 0))
    fireEvent.click(within(panel()).getByRole('button', { name: 'Apply pencil edits' }))
    await vi.waitFor(() => expect(resolver()).toBeDefined())
    const pencil = (name: string) => within(panel()).getByRole('radio', { name: new RegExp(`Paint with ${name}`) }) as HTMLButtonElement
    expect(pencil('Navy').disabled).toBe(true)
    await chooseProject(otherProject)
    await vi.waitFor(() => expect(within(panel()).queryByRole('radio', { name: /Paint with Moss/ })).not.toBeNull())
    expect(pencil('Moss').disabled).toBe(false)
    await chooseProject(project)
    await vi.waitFor(() => expect(within(panel()).queryByRole('radio', { name: /Paint with Navy/ })).not.toBeNull())
    expect(pencil('Navy').disabled).toBe(true)
    await act(async () => { resolver()!(edited); await new Promise(resolve => setTimeout(resolve, 20)) })
    await vi.waitFor(() => expect(within(panel()).getAllByRole('radio').every(button => !(button as HTMLButtonElement).disabled)).toBe(true))
  })

  it('adopts the area edit\'s own pixels at once, with that revision\'s palette, while its artifacts load', async () => {
    const applyEdits = await openEditor()
    await vi.waitFor(() => expect(workspace()?.onSelectPoint).toBeDefined())
    await vi.waitFor(() => expect(selectedWidth(0, 0)).toBe(1))
    act(() => workspace()!.onSelectPoint!([0, 0]))
    await vi.waitFor(() => expect(swatch('Orange')!.disabled).toBe(false))
    fireEvent.click(swatch('Orange')!)
    await vi.waitFor(() => expect(releaseEdited).toBeDefined())
    expect(applyEdits).toHaveBeenCalledWith(project.id, 'palette-1', [{ tool: 'reassign_region', point: [0, 0], target_label: 1 }])
    // Mid-load: the edited revision's palette, and its pixels (orange|orange, not the
    // base navy|orange) already adopted without decoding anything.
    await vi.waitFor(() => expect(swatch('Ember')).not.toBeNull())
    expect(swatch('Orange')).toBeNull()
    expect(selectedWidth(1, 0)).toBe(2)
    expect(decodedUrls).not.toContain('/artifacts/flat-edit-9')
    await act(async () => releaseEdited!())
    await vi.waitFor(() => expect(workspace()?.layers?.some(layer => layer.id === 'flat-edit-9')).toBe(true))
    expect(selectedWidth(1, 0)).toBe(2)
    expect(decodedUrls).not.toContain('/artifacts/flat-edit-9')
  })

  it('adopts a pencil edit\'s own pixels at once', async () => {
    const applyEdits = await openEditor()
    fireEvent.click(within(panel()).getByRole('button', { name: 'Pencil' }))
    await vi.waitFor(() => expect(workspace()?.onPaint).toBeDefined())
    act(() => workspace()!.onPaint!(1, 0))
    fireEvent.click(within(panel()).getByRole('button', { name: 'Apply pencil edits' }))
    await vi.waitFor(() => expect(releaseEdited).toBeDefined())
    expect(applyEdits).toHaveBeenCalledWith(project.id, 'palette-1', [{ tool: 'paint_pixels', target_label: 0, pixels: [[1, 0]] }])
    // The painted pixel is navy now, so navy fills the raster.
    fireEvent.click(within(panel()).getByRole('button', { name: 'Select areas' }))
    await vi.waitFor(() => expect(workspace()?.onSelectPoint).toBeDefined())
    expect(selectedWidth(0, 0)).toBe(2)
    expect(decodedUrls).not.toContain('/artifacts/flat-edit-9')
    await act(async () => releaseEdited!())
    await vi.waitFor(() => expect(workspace()?.layers?.some(layer => layer.id === 'flat-edit-9')).toBe(true))
    expect(selectedWidth(0, 0)).toBe(2)
    expect(decodedUrls).not.toContain('/artifacts/flat-edit-9')
  })

  it('requests each revision\'s editor state once across an area edit, as its artifacts land', async () => {
    await openEditor()
    await vi.waitFor(() => expect(workspace()?.onSelectPoint).toBeDefined())
    await vi.waitFor(() => expect(selectedWidth(0, 0)).toBe(1))
    act(() => workspace()!.onSelectPoint!([0, 0]))
    await vi.waitFor(() => expect(swatch('Orange')!.disabled).toBe(false))
    fireEvent.click(swatch('Orange')!)
    await vi.waitFor(() => expect(releaseEdited).toBeDefined())
    await act(async () => releaseEdited!())
    await vi.waitFor(() => expect(workspace()?.layers?.some(layer => layer.id === 'flat-edit-9')).toBe(true))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    const calls = (id: string) => mocks.editor.mock.calls.filter(call => call[0] === id).length
    expect(calls('palette-1')).toBe(1)
    expect(calls('edit-9')).toBe(1)
  })

  it('drops staged pencil strokes when the project is switched away, even if it comes straight back', async () => {
    await openEditor()
    // The other project's load never lands.
    mocks.project.mockImplementation(async (id: string) => id === otherProject.id
      ? new Promise(() => undefined) : { ...project, revisions: [edited, paletteRevision, revision] })
    fireEvent.click(within(panel()).getByRole('button', { name: 'Pencil' }))
    await vi.waitFor(() => expect(workspace()?.onPaint).toBeDefined())
    act(() => workspace()!.onPaint!(1, 0))
    await vi.waitFor(() => expect(panel().textContent).toContain('1 pixel painted, not yet applied.'))
    // Switch to the other project (its load never lands), then straight back.
    const select = screen.getByLabelText('Active project') as HTMLSelectElement
    await screen.findByRole('option', { name: otherProject.name })
    fireEvent.change(select, { target: { value: otherProject.id } })
    fireEvent.change(select, { target: { value: project.id } })
    await vi.waitFor(() => expect(within(panel()).queryByRole('button', { name: 'Apply pencil edits' })).not.toBeNull())
    await vi.waitFor(() => expect(workspace()?.onPaint).toBeDefined())
    expect(panel().textContent).toContain('No pixels painted yet.')
  })

  it('keeps pencil strokes painted on the adopted pixels when the revision\'s artifacts land', async () => {
    await openEditor()
    fireEvent.click(within(panel()).getByRole('button', { name: 'Pencil' }))
    await vi.waitFor(() => expect(workspace()?.onPaint).toBeDefined())
    act(() => workspace()!.onPaint!(1, 0))
    fireEvent.click(within(panel()).getByRole('button', { name: 'Apply pencil edits' }))
    await vi.waitFor(() => expect(releaseEdited).toBeDefined())
    // Paint on the adopted pixels while the edited revision's artifacts are held.
    await vi.waitFor(() => expect(within(panel()).getByRole('button', { name: 'Apply pencil edits' })).toBeTruthy())
    act(() => workspace()!.onPaint!(0, 0))
    await vi.waitFor(() => expect(panel().textContent).toContain('1 pixel painted, not yet applied.'))
    await act(async () => releaseEdited!())
    await vi.waitFor(() => expect(workspace()?.layers?.some(layer => layer.id === 'flat-edit-9')).toBe(true))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
    expect(panel().textContent).toContain('1 pixel painted, not yet applied.')
  })

  it('releases the adopted buffer once the revision loads, decoding it afresh after navigating away and back', async () => {
    await openEditor()
    await vi.waitFor(() => expect(workspace()?.onSelectPoint).toBeDefined())
    await vi.waitFor(() => expect(selectedWidth(0, 0)).toBe(1))
    act(() => workspace()!.onSelectPoint!([0, 0]))
    await vi.waitFor(() => expect(swatch('Orange')!.disabled).toBe(false))
    fireEvent.click(swatch('Orange')!)
    await vi.waitFor(() => expect(releaseEdited).toBeDefined())
    await act(async () => releaseEdited!())
    await vi.waitFor(() => expect(workspace()?.layers?.some(layer => layer.id === 'flat-edit-9')).toBe(true))
    navigate('palette-1')
    await vi.waitFor(() => expect(swatch('Orange')).not.toBeNull())
    await vi.waitFor(() => expect(selectedWidth(1, 0)).toBe(1))
    navigate('edit-9')
    // Back on the edit: its own PNG decoded (the adopted buffer is gone), with its pixels.
    await vi.waitFor(() => expect(decodedUrls).toContain('/artifacts/flat-edit-9'))
    await vi.waitFor(() => expect(swatch('Ember')).not.toBeNull())
    await vi.waitFor(() => expect(selectedWidth(1, 0)).toBe(2))
  })
})

describe('continuations that resolve after a project switch', () => {
  const paletteRevision: Revision = { ...revision, id: 'palette-1', parent_revision_id: 'revision-1', kind: 'palette', settings: { palette: {} } }
  const otherProject: Project = { ...project, id: 'project-2', slug: 'moon-forge', name: 'Moon forge' }
  const otherRevision: Revision = { ...revision, id: 'other-1', project_id: otherProject.id, kind: 'palette', settings: { palette: {} } }
  const svg = artifact('traced-overlap', 'image/svg+xml')
  const pressedView = (name: string) => within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).queryByRole('button', { name })?.getAttribute('aria-pressed')
  // Each project resolves to its own revisions and artifacts; the other project's
  // flattened raster is its own artifact.
  function twoProjects(ownArtifacts: (id: string) => Artifact[] = () => [artifact('source'), artifact('flattened')]) {
    mocks.projects.mockResolvedValue([project, otherProject])
    mocks.project.mockImplementation(async (id: string) => id === otherProject.id
      ? { ...otherProject, revisions: [otherRevision] } : { ...project, revisions: [paletteRevision, revision] })
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => id === otherRevision.id
      ? [{ ...artifact('source'), id: 'other-source' }, { ...artifact('flattened'), id: 'other-flat', revision_id: otherRevision.id }]
      : ownArtifacts(id))
  }
  async function switchToOther() {
    await chooseProject(otherProject)
    await vi.waitFor(() => expect(window.location.hash).toContain('revision=other-1'))
    await vi.waitFor(() => expect(mocks.effectiveArtifacts.mock.calls.some(call => call[0] === otherRevision.id)).toBe(true))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
  }
  // The other project stays exactly as its own load left it: selected, on its revision,
  // with nothing fetched for the revision the continuation produced.
  function expectOtherUntouched(foreignRevision: string) {
    expect((screen.getByLabelText('Active project') as HTMLSelectElement).value).toBe(otherProject.id)
    expect(window.location.hash).toContain(`project=${otherProject.id}`)
    expect(window.location.hash).toContain('revision=other-1')
    expect(mocks.effectiveArtifacts.mock.calls.some(call => call[0] === foreignRevision)).toBe(false)
    expect(mocks.editor.mock.calls.some(call => call[0] === foreignRevision)).toBe(false)
  }
  let replaceState: { mock: { calls: Parameters<History['replaceState']>[] } }
  const hashVisited = (revisionId: string) => replaceState.mock.calls.some(call => String(call[2]).includes(`revision=${revisionId}`))
  function watchHash() { replaceState = vi.spyOn(window.history, 'replaceState') }
  afterEach(() => { vi.restoreAllMocks() })

  it('leaves the shown project alone when another project\'s flatten job finishes, still spending that project\'s draft', async () => {
    let update: ((job: Job) => void) | undefined
    mocks.followJob.mockImplementation((_id: string, onJob: (job: Job) => void) => { update = onJob; return () => undefined })
    localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ palette: { initial_colours: 23 } }))
    twoProjects()
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    const run = screen.getByRole('button', { name: 'Flatten image' }) as HTMLButtonElement
    await vi.waitFor(() => expect(run.disabled).toBe(false))
    fireEvent.click(run)
    await vi.waitFor(() => expect(update).toBeDefined())
    await switchToOther()
    watchHash()
    await act(async () => update!({ ...queuedJob, kind: 'palette', status: 'succeeded', progress: 1, message: 'Flatten finished', result_revision_id: 'palette-2' }))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expectOtherUntouched('palette-2')
    expect(hashVisited('palette-2')).toBe(false)
    expect(window.location.hash).toContain('stage=flatten')
    expect(screen.getByText('Flatten finished')).toBeTruthy()
    expect(JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) || '{}').palette).toBeUndefined()
  })

  it('keeps the shown project\'s stage when a generation for another project finishes', async () => {
    let update: ((job: Job) => void) | undefined
    mocks.followJob.mockImplementation((_id: string, onJob: (job: Job) => void) => { update = onJob; return () => undefined })
    mocks.generate.mockResolvedValue({ project: { ...project, revisions: [revision] }, job: queuedJob, expanded_prompt: 'Expanded' })
    twoProjects()
    render(createElement((await import('./App')).default))
    await screen.findByRole('option', { name: otherProject.name })
    fireEvent.change(screen.getByLabelText('Short subject'), { target: { value: 'sky whale' } })
    fireEvent.submit(screen.getByLabelText('Short subject').closest('form')!)
    // Generation switches to the project it creates.
    await vi.waitFor(() => expect(window.location.hash).toContain(`project=${project.id}`))
    await vi.waitFor(() => expect(update).toBeDefined())
    await switchToOther()
    watchHash()
    await act(async () => update!({ ...queuedJob, status: 'succeeded', progress: 1, message: 'Artwork ready', result_revision_id: 'generated-1' }))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expectOtherUntouched('generated-1')
    expect(hashVisited('generated-1')).toBe(false)
    expect(window.location.hash).toContain('stage=create')
    expect(screen.getByRole('tab', { name: 'Create' }).getAttribute('aria-selected')).toBe('true')
  })

  it('drops an Inkscape reimport that resolves after a project switch', async () => {
    twoProjects(() => [artifact('source'), artifact('flattened'), svg])
    mocks.inkscapeHandoff.mockResolvedValue({ exports: [svg], shared_folder_enabled: false, guidance: 'Inspect before cutting.' })
    const { api } = await import('./api')
    let resolveReimport: ((value: Revision) => void) | undefined
    const reimport = vi.spyOn(api, 'reimportInkscape').mockImplementation(() => new Promise<Revision>(resolve => { resolveReimport = resolve }))
    render(createElement((await import('./App')).default))
    await chooseProject()
    const input = await screen.findByLabelText('Edited SVG') as HTMLInputElement
    // jsdom's FormData never sees a file set on an input, so hand the form its file.
    const edited = new File(['<svg/>'], 'edited.svg', { type: 'image/svg+xml' })
    const formGet = FormData.prototype.get
    vi.spyOn(FormData.prototype, 'get').mockImplementation(function (this: FormData, name: string) { return name === 'svg' ? edited : formGet.call(this, name) })
    fireEvent.submit(input.closest('form')!)
    await vi.waitFor(() => expect(resolveReimport).toBeDefined())
    expect(reimport).toHaveBeenCalledWith(project.id, 'palette-1', edited)
    await switchToOther()
    watchHash()
    await act(async () => { resolveReimport!({ ...revision, id: 'reimport-1', parent_revision_id: 'palette-1', kind: 'inkscape-reimport' }) })
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expectOtherUntouched('reimport-1')
    expect(hashVisited('reimport-1')).toBe(false)
    expect(screen.queryByText('Edited SVG validated and saved as a new revision.')).toBeNull()
  })

  it('drops a palette edit committed after a project switch', async () => {
    twoProjects()
    window.history.replaceState(null, '', `/#stage=palette&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(editorProps.current).not.toBeNull())
    // The commit callback as it stood when the recolour was started in the first project.
    const commitFromFirst = editorProps.current!.onCommit
    await switchToOther()
    await vi.waitFor(() => expect(pressedView('Flattened')).toBe('true'))
    watchHash()
    await act(async () => commitFromFirst('edit-1'))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expectOtherUntouched('edit-1')
    expect(hashVisited('edit-1')).toBe(false)
    expect(pressedView('Edited')).toBeUndefined()
  })

  it('keeps the shown project selected when deleting another project finishes after a switch', async () => {
    twoProjects()
    let finishDelete: (() => void) | undefined
    mocks.deleteProject.mockImplementation(() => new Promise<void>(resolve => { finishDelete = resolve }))
    render(createElement((await import('./App')).default))
    await chooseProject()
    await vi.waitFor(() => expect(window.location.hash).toContain('revision=palette-1'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete project' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Delete project' }))
    await vi.waitFor(() => expect(finishDelete).toBeDefined())
    await switchToOther()
    await act(async () => finishDelete!())
    await vi.waitFor(() => expect(screen.queryByRole('option', { name: project.name })).toBeNull())
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect((screen.getByLabelText('Active project') as HTMLSelectElement).value).toBe(otherProject.id)
    expect(window.location.hash).toContain(`project=${otherProject.id}`)
    expect(window.location.hash).toContain('revision=other-1')
    expect(screen.getByRole('option', { name: otherProject.name })).toBeTruthy()
  })

  it('shows no shared-folder confirmation for another project\'s save', async () => {
    twoProjects(() => [artifact('source'), artifact('flattened'), svg])
    mocks.effectiveArtifacts.mockImplementation(async () => [artifact('source'), artifact('flattened'), svg])
    mocks.inkscapeHandoff.mockResolvedValue({ exports: [svg], shared_folder_enabled: true, guidance: 'Inspect before cutting.' })
    let finishSave: ((value: { filename: string; shared_path: string }) => void) | undefined
    mocks.saveForInkscape.mockImplementation(() => new Promise(resolve => { finishSave = resolve }))
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(await screen.findByRole('button', { name: 'Save SVG to shared folder' }))
    await vi.waitFor(() => expect(finishSave).toBeDefined())
    await switchToOther()
    await screen.findByRole('button', { name: 'Save SVG to shared folder' })
    await act(async () => finishSave!({ filename: 'traced-overlap.svg', shared_path: '/shared/traced-overlap.svg' }))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect(screen.queryByText('Saved traced-overlap.svg to the shared folder.')).toBeNull()
  })

  it('names the project in a shared-folder save failure reported after a switch, and only then', async () => {
    twoProjects(() => [artifact('source'), artifact('flattened'), svg])
    mocks.effectiveArtifacts.mockImplementation(async () => [artifact('source'), artifact('flattened'), svg])
    mocks.inkscapeHandoff.mockResolvedValue({ exports: [svg], shared_folder_enabled: true, guidance: 'Inspect before cutting.' })
    mocks.saveForInkscape.mockRejectedValueOnce(new Error('disk full'))
    render(createElement((await import('./App')).default))
    await chooseProject()
    fireEvent.click(await screen.findByRole('button', { name: 'Save SVG to shared folder' }))
    await vi.waitFor(() => expect(screen.getByRole('alert').textContent).toBe('Error: disk full'))

    let failSave: ((cause: Error) => void) | undefined
    mocks.saveForInkscape.mockImplementation(() => new Promise((_resolve, reject) => { failSave = reject }))
    fireEvent.click(screen.getByRole('button', { name: 'Save SVG to shared folder' }))
    await vi.waitFor(() => expect(failSave).toBeDefined())
    await switchToOther()
    await act(async () => failSave!(new Error('disk full')))
    await vi.waitFor(() => expect(screen.getByRole('alert').textContent)
      .toBe(`${project.name}: could not save ${svg.filename} to the shared folder: Error: disk full`))
  })

  it('names the project in an Inkscape reimport failure reported after a switch, and only then', async () => {
    twoProjects(() => [artifact('source'), artifact('flattened'), svg])
    mocks.inkscapeHandoff.mockResolvedValue({ exports: [svg], shared_folder_enabled: false, guidance: 'Inspect before cutting.' })
    const { api } = await import('./api')
    const reimport = vi.spyOn(api, 'reimportInkscape').mockRejectedValueOnce(new Error('invalid SVG'))
    render(createElement((await import('./App')).default))
    await chooseProject()
    const input = await screen.findByLabelText('Edited SVG') as HTMLInputElement
    // jsdom's FormData never sees a file set on an input, so hand the form its file.
    const edited = new File(['<svg/>'], 'edited.svg', { type: 'image/svg+xml' })
    const formGet = FormData.prototype.get
    vi.spyOn(FormData.prototype, 'get').mockImplementation(function (this: FormData, name: string) { return name === 'svg' ? edited : formGet.call(this, name) })
    fireEvent.submit(input.closest('form')!)
    await vi.waitFor(() => expect(screen.getByRole('alert').textContent).toBe('Error: invalid SVG'))

    let failReimport: ((cause: Error) => void) | undefined
    reimport.mockImplementation(() => new Promise((_resolve, reject) => { failReimport = reject }))
    fireEvent.submit(input.closest('form')!)
    await vi.waitFor(() => expect(failReimport).toBeDefined())
    await switchToOther()
    await act(async () => failReimport!(new Error('invalid SVG')))
    await vi.waitFor(() => expect(screen.getByRole('alert').textContent)
      .toBe(`${project.name}: could not reimport the edited SVG: Error: invalid SVG`))
  })
})

describe('stepping trace runs while their artifacts load', () => {
  const paletteRevision: Revision = { ...revision, id: 'palette-1', kind: 'palette', settings: { palette: {} } }
  const solid: Revision = { ...revision, id: 'trace-8', parent_revision_id: 'palette-1', kind: 'trace', created_at: '2026-01-02', settings: { trace: { overlap: 0, solid_underlayers: true } } }
  const edge: Revision = { ...revision, id: 'trace-9', parent_revision_id: 'trace-8', kind: 'trace', created_at: '2026-01-03', settings: { trace: { overlap: 0, solid_underlayers: false } } }
  const run = (id: string) => [artifact('source'), artifact('flattened'), artifact('segmentation', 'application/octet-stream'),
    { ...artifact('traced-edge', 'image/svg+xml'), id: `${id}-edge`, revision_id: id },
    { ...artifact('traced-overlap', 'image/svg+xml'), id: `${id}-overlap`, revision_id: id }]
  // What the canvas would title itself with on every render: the preferred layer's
  // name, or the neutral loading title while loading.
  let titles: string[], preferred: (string | undefined)[], captured: CapturedWorkspace | null, release: (() => void) | undefined

  beforeEach(() => {
    titles = []; preferred = []; captured = null; release = undefined
    Object.defineProperty(workspaceProps, 'current', { configurable: true, get: () => captured, set: value => {
      captured = value
      if (!value) return
      preferred.push(value.preferredLayerId)
      titles.push(value.loading ? 'Loading…' : value.layers?.find((layer: { id: string }) => layer.id === value.preferredLayerId)?.name ?? '')
    } })
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [edge, solid, paletteRevision, revision] })
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => {
      if (id === 'trace-8') await new Promise<void>(resolve => { release = resolve })
      return run(id)
    })
  })
  afterEach(() => { Object.defineProperty(workspaceProps, 'current', { configurable: true, writable: true, value: null }) })

  async function openNewestRun(view: string) {
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-9`)
    render(createElement((await import('./App')).default))
    const panel = screen.getByRole('tabpanel', { name: 'Trace' })
    await within(panel).findByText('Iteration 2 of 2')
    await screen.findAllByRole('group', { name: 'Artwork view' })
    fireEvent.click(within(screen.getAllByRole('group', { name: 'Artwork view' })[0]).getByRole('button', { name: view }))
    return panel
  }

  it('never titles the canvas with the previous run while the stepped-to run loads', async () => {
    const panel = await openNewestRun('Traced')
    await vi.waitFor(() => expect(captured?.preferredLayerId).toBe('trace-9-edge'))
    expect(titles.at(-1)).toBe('Traced SVG · edge to edge')
    titles = []
    fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
    await vi.waitFor(() => expect(release).toBeDefined())
    await act(async () => release!())
    await vi.waitFor(() => expect(captured?.preferredLayerId).toBe('trace-8-overlap'))
    await vi.waitFor(() => expect(titles.at(-1)).toBe('Traced SVG · solid underlayers'))
    expect(titles).not.toContain('Traced SVG · edge to edge')
    expect(new Set(titles)).toEqual(new Set(['Loading…', 'Traced SVG · solid underlayers']))
  })

  it('stays on the Source view until the stepped-to run has loaded, never showing the previous run', async () => {
    const panel = await openNewestRun('Source')
    await vi.waitFor(() => expect(captured?.preferredLayerId).toBe('source-id'))
    preferred = []
    fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
    await vi.waitFor(() => expect(release).toBeDefined())
    expect(preferred.every(id => id === 'source-id')).toBe(true)
    await act(async () => release!())
    await vi.waitFor(() => expect(captured?.preferredLayerId).toBe('trace-8-overlap'))
    expect(preferred).not.toContain('trace-9-edge')
    expect(preferred).not.toContain('trace-9-overlap')
  })

  it('keeps the Trace panel still across a step: same heading, same controls, no inserted reason', async () => {
    const panel = await openNewestRun('Traced')
    await vi.waitFor(() => expect(captured?.preferredLayerId).toBe('trace-9-edge'))
    const trace = () => within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
    await vi.waitFor(() => expect(trace().disabled).toBe(false))
    const heading = () => panel.querySelector('.section-heading span')?.textContent
    // Every visible element above the stepper (the run's own settings below it may differ).
    const layout = () => {
      const nodes = [...panel.querySelectorAll('*')].filter(node => !node.closest('.visually-hidden'))
      const stepper = nodes.indexOf(within(panel).getByLabelText('Show the previous trace run'))
      return nodes.slice(0, stepper).map(node => `${node.tagName}.${node.className}`)
    }
    const before = layout()
    expect(heading()).toBe('Ready')
    fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
    await vi.waitFor(() => expect(release).toBeDefined())
    // Mid-load: locked, with the reason for assistive tech only; nothing shifts.
    expect(trace().disabled).toBe(true)
    const reason = document.getElementById(trace().getAttribute('aria-describedby') ?? '')
    expect(reason?.textContent).toBe('Loading the selected revision…')
    expect(reason?.className).toBe('visually-hidden')
    expect(panel.querySelector('.lock-reason')).toBeNull()
    expect(heading()).toBe('Ready')
    expect(layout()).toEqual(before)
    await act(async () => release!())
    await within(panel).findByText('Iteration 1 of 2')
    await vi.waitFor(() => expect(trace().disabled).toBe(false))
    expect(trace().getAttribute('aria-describedby')).toBeNull()
    expect(heading()).toBe('Ready')
    expect(layout()).toEqual(before)
  })

  it('never offers the Inkscape handoff of a revision whose artifacts are not yet shown', async () => {
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => {
      if (id === 'palette-1') {
        await new Promise<void>(resolve => { release = resolve })
        return [artifact('source'), artifact('flattened'), artifact('segmentation', 'application/octet-stream')]
      }
      return run(id)
    })
    await openNewestRun('Traced')
    await vi.waitFor(() => expect(mocks.inkscapeHandoff).toHaveBeenCalledWith('trace-9', expect.anything()))
    await screen.findByRole('button', { name: 'Validate and reimport' })
    act(() => {
      window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=palette-1`)
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    await vi.waitFor(() => expect(release).toBeDefined())
    // The untraced revision is selected but not loaded: no handoff, no reimport form.
    expect(screen.queryByRole('button', { name: 'Validate and reimport' })).toBeNull()
    await act(async () => release!())
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
    expect(screen.queryByRole('button', { name: 'Validate and reimport' })).toBeNull()
    expect(mocks.inkscapeHandoff).not.toHaveBeenCalledWith('palette-1', expect.anything())
  })

  it('keeps the solid-underlayer layer order listed through a step, never claiming no flatten', async () => {
    const solidNewest: Revision = { ...edge, settings: { trace: { overlap: 0, solid_underlayers: true } } }
    mocks.project.mockResolvedValue({ ...project, revisions: [solidNewest, solid, paletteRevision, revision] })
    mocks.editor.mockImplementation(async (id: string) => ({ width: 40, height: 30, protected_pixels: 0, palette: [
      { index: 0, name: `Navy ${id}`, hex: '#0F233C', rgb: [15, 35, 60], share_percent: 60 },
      { index: 1, name: `Orange ${id}`, hex: '#E15F2D', rgb: [225, 95, 45], share_percent: 40 },
    ] }))
    const panel = await openNewestRun('Traced')
    await within(panel).findByText('Navy trace-9')
    fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
    await vi.waitFor(() => expect(release).toBeDefined())
    // Mid-load: the shown run's layer order stays listed; no false empty state.
    expect(within(panel).getByText('Navy trace-9')).toBeTruthy()
    expect(panel.textContent).not.toContain('Flatten this revision')
    await act(async () => release!())
    // The new run's palette replaces it once it arrives.
    await within(panel).findByText('Navy trace-8')
    expect(panel.textContent).not.toContain('Flatten this revision')
  })

  describe('layer order against each run\'s own palette', () => {
    // The two runs' palettes share no hex value.
    const palettes: Record<string, { index: number; name: string; hex: string; rgb: [number, number, number]; share_percent: number }[]> = {
      'trace-9': [
        { index: 0, name: 'Navy', hex: '#0F233C', rgb: [15, 35, 60], share_percent: 60 },
        { index: 1, name: 'Orange', hex: '#E15F2D', rgb: [225, 95, 45], share_percent: 40 },
      ],
      'trace-8': [
        { index: 0, name: 'Teal', hex: '#1A7F7A', rgb: [26, 127, 122], share_percent: 55 },
        { index: 1, name: 'Sand', hex: '#D8C8A0', rgb: [216, 200, 160], share_percent: 45 },
      ],
    }
    let failEditor: Set<string>, holdEditor: Set<string>, releaseEditor: (() => void)[]
    // The palette index of the colour each run's active cleanup removes (EditorState.removed_colour_index).
    const removedIndex: Record<string, number> = {}
    beforeEach(() => {
      failEditor = new Set(); holdEditor = new Set(); releaseEditor = []
      mocks.project.mockResolvedValue({ ...project, revisions: [{ ...edge, settings: { trace: { overlap: 0, solid_underlayers: true } } }, solid, paletteRevision, revision] })
      // Held reads settle together on release, or reject once their signal aborts.
      mocks.editor.mockImplementation(async (id: string, signal?: AbortSignal) => {
        if (holdEditor.has(id)) await new Promise<void>((resolve, reject) => {
          releaseEditor.push(resolve)
          signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')), { once: true })
        })
        if (failEditor.has(id)) throw new Error('editor unavailable')
        return { width: 40, height: 30, protected_pixels: 0, palette: palettes[id] ?? [], removed_colour_index: removedIndex[id] ?? null }
      })
      // A traceable run's automatic order is palette order unless a test sets one.
      mocks.automaticLayerOrder.mockImplementation(async (id: string) => ({ automatic_layer_order: automatic[id] ?? palettes[id]?.map(row => row.hex) ?? null }))
    })
    // The tracer's automatic bottom-to-top order per run, when a test sets one.
    const automatic: Record<string, string[]> = {}
    afterEach(() => { for (const key of Object.keys(automatic)) delete automatic[key] })
    const listed = (panel: HTMLElement) => [...panel.querySelectorAll('.layer-order-name')].map(node => node.textContent)
    const previewOrder = () => (captured as { livePreview?: { settings: { layer_order: string[] | null } } } | null)?.livePreview?.settings.layer_order
    // The top listed colour's move-down button (its move-up is always disabled).
    const moveDown = (panel: HTMLElement, name: string) => within(panel).getByRole('button', { name: `Move ${name} down a layer` }) as HTMLButtonElement

    it('cannot reorder the listed colours while they belong to the previous run', async () => {
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect(moveDown(panel, 'Orange').disabled).toBe(false))
      fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
      await vi.waitFor(() => expect(release).toBeDefined())
      // Still listed (no jump), but locked with the reason.
      const up = moveDown(panel, 'Orange')
      expect(up.disabled).toBe(true)
      expect(document.getElementById(up.getAttribute('aria-describedby') ?? '')?.textContent).toBe('Loading the selected revision…')
      await act(async () => release!())
      await vi.waitFor(() => expect(moveDown(panel, 'Sand').disabled).toBe(false))
      expect(within(panel).queryByText('Orange')).toBeNull()
    })

    it('narrows a stale stored order to the run\'s palette before previewing or tracing', async () => {
      localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { solid_underlayers: true, layer_order: ['#1A7F7A', '#e15f2d', '#0F233C', 'Orange'] } }))
      mocks.startPipeline.mockResolvedValue({ ...queuedJob, kind: 'trace' })
      const panel = await openNewestRun('Traced')
      // Stored bottom-to-top Orange, Navy: Navy is listed on top.
      await vi.waitFor(() => expect(moveDown(panel, 'Navy').disabled).toBe(false))
      // The other run's Teal is dropped; Orange (by hex and by name) is kept once, in order.
      const narrowed = ['#E15F2D', '#0F233C']
      await vi.waitFor(() => expect((captured as { livePreview?: { settings: { layer_order: string[] | null } } } | null)?.livePreview?.settings.layer_order).toEqual(narrowed))
      const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      fireEvent.click(run)
      await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalled())
      expect(mocks.startPipeline.mock.calls[0][3].trace.layer_order).toEqual(narrowed)
    })

    it('locks the order when the run\'s palette cannot be read, and reads it afresh before tracing', async () => {
      failEditor.add('trace-9')
      localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { solid_underlayers: true, layer_order: ['#1A7F7A', '#E15F2D'] } }))
      mocks.startPipeline.mockResolvedValue({ ...queuedJob, kind: 'trace' })
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect(within(panel).getByText('Palette unavailable: Error: editor unavailable')).toBeTruthy())
      // Without the run's palette the preview is never handed the unchecked order.
      expect(previewOrder()).toBeNull()
      // Reading it again for the job still fails: no job starts with an unchecked order.
      const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      fireEvent.click(run)
      expect((await screen.findByRole('alert')).textContent).toContain('editor unavailable')
      expect(mocks.startPipeline).not.toHaveBeenCalled()
      // Once readable, the job reads the palette itself and narrows the order.
      failEditor.clear()
      fireEvent.click(run)
      await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalled())
      expect(mocks.startPipeline.mock.calls[0][3].trace.layer_order).toEqual(['#E15F2D'])
    })

    it('shows the real empty state on a revision with no flatten', async () => {
      mocks.effectiveArtifacts.mockImplementation(async (id: string) => id === 'revision-1' ? [artifact('source')] : run(id))
      localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { solid_underlayers: true } }))
      window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=revision-1`)
      render(createElement((await import('./App')).default))
      const panel = screen.getByRole('tabpanel', { name: 'Trace' })
      await vi.waitFor(() => expect(panel.textContent).toContain('Flatten this revision to choose a layer order.'))
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
      expect(panel.textContent).toContain('Flatten this revision to choose a layer order.')
      expect(panel.textContent).not.toContain('Loading the selected revision')
    })

    it('shows exactly the order it sends, whatever the case or \'#\' of the stored entries', async () => {
      localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { solid_underlayers: true, layer_order: ['e15f2d', 'navy'] } }))
      const panel = await openNewestRun('Traced')
      // Bottom-to-top Orange, Navy: listed top first as Navy, Orange (automatic would be Orange, Navy).
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange']))
      await vi.waitFor(() => expect(previewOrder()).toEqual(['#E15F2D', '#0F233C']))
    })

    it('leaves the colour Cleanup removes out of the list, the preview and the job', async () => {
      palettes['trace-9'].push({ index: 2, name: 'Teal', hex: '#1A7F7A', rgb: [26, 127, 122], share_percent: 10 })
      // The run's palette says which colour the cleanup removed by index, not by the name
      // it recorded: a trace's colour names gave that name ('navy') to another colour since.
      removedIndex['trace-9'] = 1
      try {
        const cleanupRun: Revision = { ...revision, id: 'cleanup-1', parent_revision_id: 'palette-1', kind: 'cleanup', created_at: '2026-01-02', settings: { cleanup: { remove_colour: 'navy' }, removed_colour_index: 1, stale_concerns: ['trace', 'validation', 'export'] } }
        const newest: Revision = { ...edge, parent_revision_id: 'cleanup-1', settings: { trace: { overlap: 0, solid_underlayers: true } } }
        mocks.project.mockResolvedValue({ ...project, revisions: [newest, cleanupRun, paletteRevision, revision] })
        // A stored order still naming the removed colour.
        localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { solid_underlayers: true, layer_order: ['#E15F2D', '#0F233C', '#1A7F7A'] } }))
        mocks.startPipeline.mockResolvedValue({ ...queuedJob, kind: 'trace' })
        window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-9`)
        render(createElement((await import('./App')).default))
        const panel = screen.getByRole('tabpanel', { name: 'Trace' })
        await vi.waitFor(() => expect(listed(panel)).toEqual(['Teal', 'Navy']))
        expect(panel.textContent).toContain('Orange is removed in Cleanup and gets no layer.')
        await vi.waitFor(() => expect(previewOrder()).toEqual(['#0F233C', '#1A7F7A']))
        // A reorder writes only colours that get a layer.
        fireEvent.click(within(panel).getByRole('button', { name: 'Move Teal down a layer' }))
        await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Teal']))
        await vi.waitFor(() => expect(previewOrder()).toEqual(['#1A7F7A', '#0F233C']))
        const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
        await vi.waitFor(() => expect(run.disabled).toBe(false))
        fireEvent.click(run)
        await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalled())
        expect(mocks.startPipeline.mock.calls[0][3].trace.layer_order).toEqual(['#1A7F7A', '#0F233C'])
      } finally { palettes['trace-9'].pop(); delete removedIndex['trace-9'] }
    })

    it('starts one job from a double click while it reads the palette, and none if the revision changes meanwhile', async () => {
      holdEditor.add('trace-9')
      localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { solid_underlayers: true, layer_order: ['#E15F2D'] } }))
      mocks.startPipeline.mockResolvedValue({ ...queuedJob, kind: 'trace' })
      const panel = await openNewestRun('Traced')
      const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      fireEvent.click(run)
      fireEvent.click(run)
      expect(run.disabled).toBe(true)
      await act(async () => { releaseEditor.splice(0).forEach(resolve => resolve()) })
      await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalledTimes(1))
      expect(mocks.startPipeline.mock.calls[0][3].trace.layer_order).toEqual(['#E15F2D'])
    })

    it('starts nothing when the revision changes while the job reads its palette', async () => {
      holdEditor.add('trace-9')
      localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { solid_underlayers: true, layer_order: ['#E15F2D'] } }))
      const panel = await openNewestRun('Traced')
      const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      fireEvent.click(run)
      fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
      await vi.waitFor(() => expect(release).toBeDefined())
      await act(async () => { releaseEditor.splice(0).forEach(resolve => resolve()); release!() })
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
      expect(mocks.startPipeline).not.toHaveBeenCalled()
      expect(screen.queryByRole('alert')).toBeNull()
    })

    it('reads no automatic order and lists none while colours meet edge to edge, then reads it once order matters', async () => {
      mocks.project.mockResolvedValue({ ...project, revisions: [{ ...edge, settings: { trace: { overlap: 0, solid_underlayers: false } } }, solid, paletteRevision, revision] })
      automatic['trace-9'] = ['#E15F2D', '#0F233C']
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect(mocks.editor.mock.calls.some(call => call[0] === 'trace-9')).toBe(true))
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
      expect(mocks.automaticLayerOrder).not.toHaveBeenCalled()
      expect(panel.querySelector('.layer-order-list')).toBeNull()
      fireEvent.click(within(panel).getByRole('checkbox', { name: /Solid underlayers/ }))
      await vi.waitFor(() => expect(mocks.automaticLayerOrder).toHaveBeenCalledWith('trace-9', expect.anything()))
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange']))
      // Read once per revision: switching it off and on again does not re-read it.
      fireEvent.click(within(panel).getByRole('checkbox', { name: /Solid underlayers/ }))
      expect(panel.querySelector('.layer-order-list')).toBeNull()
      fireEvent.click(within(panel).getByRole('checkbox', { name: /Solid underlayers/ }))
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange']))
      expect(mocks.automaticLayerOrder).toHaveBeenCalledTimes(1)
    })

    it('keeps the list locked until the automatic order arrives once order starts to matter', async () => {
      mocks.project.mockResolvedValue({ ...project, revisions: [{ ...edge, settings: { trace: { overlap: 0, solid_underlayers: false } } }, solid, paletteRevision, revision] })
      let deliver: ((value: { automatic_layer_order: string[] | null }) => void) | undefined
      mocks.automaticLayerOrder.mockImplementation(() => new Promise(resolve => { deliver = resolve }))
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect(mocks.editor.mock.calls.some(call => call[0] === 'trace-9')).toBe(true))
      fireEvent.click(within(panel).getByRole('checkbox', { name: /Solid underlayers/ }))
      await vi.waitFor(() => expect(deliver).toBeDefined())
      // Palette order is listed meanwhile, but it is not the automatic order: no reordering.
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Orange', 'Navy']))
      const down = moveDown(panel, 'Orange')
      expect(down.disabled).toBe(true)
      expect(document.getElementById(down.getAttribute('aria-describedby') ?? '')?.textContent).toBe('Loading automatic order…')
      expect(panel.textContent).not.toContain('Automatic order until you reorder')
      await act(async () => deliver!({ automatic_layer_order: ['#E15F2D', '#0F233C'] }))
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange']))
      expect(moveDown(panel, 'Navy').disabled).toBe(false)
    })

    it('shows a failed automatic-order read as the list\'s lock, with a Retry that reads it again', async () => {
      automatic['trace-9'] = ['#E15F2D', '#0F233C']
      mocks.automaticLayerOrder.mockRejectedValueOnce(new Error('order unavailable'))
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect(panel.textContent).toContain('Automatic order unavailable: Error: order unavailable'))
      expect(moveDown(panel, 'Orange').disabled).toBe(true)
      expect(screen.queryByRole('alert')).toBeNull()
      fireEvent.click(within(panel).getByRole('button', { name: 'Retry' }))
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange']))
      expect(moveDown(panel, 'Navy').disabled).toBe(false)
      expect(mocks.automaticLayerOrder).toHaveBeenCalledTimes(2)
    })

    it('reads each revision\'s automatic order once, stepping away and back', async () => {
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect(mocks.automaticLayerOrder).toHaveBeenCalledWith('trace-9', expect.anything()))
      fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
      await vi.waitFor(() => expect(release).toBeDefined())
      await act(async () => release!())
      await vi.waitFor(() => expect(mocks.automaticLayerOrder).toHaveBeenCalledWith('trace-8', expect.anything()))
      fireEvent.click(within(panel).getByLabelText('Show the next trace run'))
      await within(panel).findByText('Iteration 2 of 2')
      await vi.waitFor(() => expect(moveDown(panel, 'Orange').disabled).toBe(false))
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
      expect(mocks.automaticLayerOrder.mock.calls.map(call => call[0])).toEqual(['trace-9', 'trace-8'])
    })

    it('sends no layer order while order cannot matter, and keeps the stored one for when it does', async () => {
      mocks.project.mockResolvedValue({ ...project, revisions: [{ ...edge, settings: { trace: { overlap: 0, solid_underlayers: false } } }, solid, paletteRevision, revision] })
      localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { layer_order: ['#E15F2D'] } }))
      let update: ((job: Job) => void) | undefined
      mocks.followJob.mockImplementation((_id: string, onJob: (job: Job) => void) => { update = onJob; return () => undefined })
      mocks.startPipeline.mockResolvedValue({ ...queuedJob, id: 'job-edge', kind: 'trace' })
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect((captured as { livePreview?: unknown } | null)?.livePreview).toBeDefined())
      expect(previewOrder()).toBeNull()
      const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      fireEvent.click(run)
      await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalled())
      expect(mocks.startPipeline.mock.calls[0][3].trace.layer_order).toBeNull()
      await vi.waitFor(() => expect(update).toBeDefined())
      await act(async () => update?.({ ...queuedJob, id: 'job-edge', kind: 'trace', status: 'succeeded', progress: 1, result_revision_id: 'trace-9' }))
      // The run spent the draft but not the override it left out.
      expect(JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) ?? '{}').trace).toEqual({ layer_order: ['#E15F2D'] })
      fireEvent.click(within(panel).getByRole('checkbox', { name: /Solid underlayers/ }))
      await vi.waitFor(() => expect(previewOrder()).toEqual(['#E15F2D']))
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange']))
    })

    it('keeps an older run\'s recorded layer order for the next form when a run from it leaves the order out', async () => {
      mocks.project.mockResolvedValue({ ...project, revisions: [{ ...edge, settings: { trace: { overlap: 0, solid_underlayers: true } } }, { ...solid, settings: { trace: { overlap: 0, solid_underlayers: true, layer_order: ['#1A7F7A'] } } }, paletteRevision, revision] })
      mocks.startPipeline.mockResolvedValue({ ...queuedJob, id: 'job-older', kind: 'trace' })
      const panel = await openNewestRun('Traced')
      fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
      await vi.waitFor(() => expect(release).toBeDefined())
      await act(async () => release!())
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Sand', 'Teal']))
      fireEvent.click(within(panel).getByRole('checkbox', { name: /Solid underlayers/ }))
      const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      fireEvent.click(run)
      await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalled())
      expect(mocks.startPipeline.mock.calls[0][2]).toBe('trace-8')
      expect(mocks.startPipeline.mock.calls[0][3].trace.layer_order).toBeNull()
      expect(JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) ?? '{}').trace).toEqual({ layer_order: ['#1A7F7A'] })
    })

    it('keeps a withheld recorded layer order through a reload while the run is still going', async () => {
      palettes['trace-10'] = palettes['trace-9']
      try {
        // The newest run recorded an override; its form turns order off and runs.
        mocks.project.mockResolvedValue({ ...project, revisions: [{ ...edge, settings: { trace: { overlap: 0, solid_underlayers: true, layer_order: ['#E15F2D'] } } }, solid, paletteRevision, revision] })
        mocks.startPipeline.mockResolvedValue({ ...queuedJob, id: 'job-reload', kind: 'trace' })
        const panel = await openNewestRun('Traced')
        await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange']))
        fireEvent.click(within(panel).getByRole('checkbox', { name: /Solid underlayers/ }))
        const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
        await vi.waitFor(() => expect(run.disabled).toBe(false))
        fireEvent.click(run)
        await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalled())
        expect(mocks.startPipeline.mock.calls[0][3].trace.layer_order).toBeNull()
        // Reload mid-run; the run then succeeds, recording no layer order.
        cleanup()
        const traced: Revision = { ...edge, id: 'trace-10', parent_revision_id: 'trace-9', created_at: '2026-01-04', settings: { trace: { overlap: 0, solid_underlayers: false, layer_order: null } } }
        mocks.project.mockResolvedValue({ ...project, revisions: [traced, { ...edge, settings: { trace: { overlap: 0, solid_underlayers: true, layer_order: ['#E15F2D'] } } }, solid, paletteRevision, revision] })
        window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-10`)
        render(createElement((await import('./App')).default))
        const reloaded = screen.getByRole('tabpanel', { name: 'Trace' })
        await within(reloaded).findByText('Iteration 3 of 3')
        fireEvent.click(within(reloaded).getByRole('checkbox', { name: /Solid underlayers/ }))
        await vi.waitFor(() => expect(listed(reloaded)).toEqual(['Navy', 'Orange']))
        await vi.waitFor(() => expect(previewOrder()).toEqual(['#E15F2D']))
      } finally { delete palettes['trace-10'] }
    })

    const storedTraceDraft = () => JSON.parse(localStorage.getItem(`vinylgen.project-settings.${project.id}`) ?? '{}').trace
    const recordedOrder = (order: string[]) => ({ ...edge, settings: { trace: { overlap: 0, solid_underlayers: true, layer_order: order } } })
    async function runWithOrderOff(panel: HTMLElement, jobId: string) {
      let update: ((job: Job) => void) | undefined
      mocks.followJob.mockImplementation((_id: string, onJob: (job: Job) => void) => { update = onJob; return () => undefined })
      mocks.startPipeline.mockResolvedValue({ ...queuedJob, id: jobId, kind: 'trace' })
      fireEvent.click(within(panel).getByRole('checkbox', { name: /Solid underlayers/ }))
      const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      fireEvent.click(run)
      await vi.waitFor(() => expect(update).toBeDefined())
      expect(mocks.startPipeline.mock.calls[0][3].trace.layer_order).toBeNull()
      return (job: Partial<Job>) => act(async () => update!({ ...queuedJob, id: jobId, kind: 'trace', ...job }))
    }

    it('leaves the newest run\'s pending draft order alone when a run from an older run withholds its own', async () => {
      mocks.project.mockResolvedValue({ ...project, revisions: [{ ...edge, settings: { trace: { overlap: 0, solid_underlayers: true } } }, { ...solid, settings: { trace: { overlap: 0, solid_underlayers: true, layer_order: ['#1A7F7A'] } } }, paletteRevision, revision] })
      localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { layer_order: ['#E15F2D'] } }))
      const panel = await openNewestRun('Traced')
      fireEvent.click(within(panel).getByLabelText('Show the previous trace run'))
      await vi.waitFor(() => expect(release).toBeDefined())
      await act(async () => release!())
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Sand', 'Teal']))
      await runWithOrderOff(panel, 'job-older')
      expect(mocks.startPipeline.mock.calls[0][2]).toBe('trace-8')
      expect(storedTraceDraft()).toEqual({ layer_order: ['#E15F2D'] })
    })

    it('keeps an order the user saved during the run instead of restoring the withheld one', async () => {
      mocks.project.mockResolvedValue({ ...project, revisions: [recordedOrder(['#E15F2D']), solid, paletteRevision, revision] })
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange']))
      const finish = await runWithOrderOff(panel, 'job-edited')
      expect(storedTraceDraft()).toEqual({ layer_order: ['#E15F2D'] })
      // Mid-run: order back on, reset to automatic, and saved to the draft.
      fireEvent.click(within(panel).getByRole('checkbox', { name: /Solid underlayers/ }))
      const reset = await within(panel).findByRole('button', { name: 'Reset to automatic' })
      fireEvent.click(reset)
      fireEvent.focusOut(within(panel).getByRole('checkbox', { name: /Solid underlayers/ }))
      await vi.waitFor(() => expect(storedTraceDraft()?.layer_order).toBeNull())
      await finish({ status: 'succeeded', progress: 1, result_revision_id: 'trace-9' })
      expect(storedTraceDraft()).toEqual({ layer_order: null })
    })

    it('leaves the trace draft untouched by a run where order matters', async () => {
      mocks.project.mockResolvedValue({ ...project, revisions: [recordedOrder(['#E15F2D']), solid, paletteRevision, revision] })
      mocks.startPipeline.mockResolvedValue({ ...queuedJob, id: 'job-solid', kind: 'trace' })
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange']))
      const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      fireEvent.click(run)
      await vi.waitFor(() => expect(mocks.startPipeline).toHaveBeenCalled())
      expect(mocks.startPipeline.mock.calls[0][3].trace.layer_order).toEqual(['#E15F2D'])
      expect(localStorage.getItem(`vinylgen.project-settings.${project.id}`)).toBeNull()
    })

    it('keeps the draft written before the start when the trace job fails', async () => {
      mocks.project.mockResolvedValue({ ...project, revisions: [recordedOrder(['#E15F2D']), solid, paletteRevision, revision] })
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange']))
      const finish = await runWithOrderOff(panel, 'job-failed')
      await finish({ status: 'failed', progress: 1, error: 'trace failed' })
      expect(storedTraceDraft()).toEqual({ layer_order: ['#E15F2D'] })
    })

    it('keeps the list locked when the revision has no automatic order (it cannot be traced as it stands)', async () => {
      mocks.automaticLayerOrder.mockResolvedValueOnce({ automatic_layer_order: null })
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect(panel.textContent).toContain('Automatic order unavailable: this revision cannot be traced as it stands.'))
      expect(moveDown(panel, 'Orange').disabled).toBe(true)
      expect(panel.textContent).not.toContain('Automatic order until you reorder')
    })

    it('lists the layer order, in the automatic order, with an overlap and no solid underlayers', async () => {
      mocks.project.mockResolvedValue({ ...project, revisions: [{ ...edge, settings: { trace: { overlap: 1, solid_underlayers: false } } }, solid, paletteRevision, revision] })
      automatic['trace-9'] = ['#E15F2D', '#0F233C']
      const panel = await openNewestRun('Traced')
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange']))
      expect(mocks.automaticLayerOrder).toHaveBeenCalledWith('trace-9', expect.anything())
    })

    it('lists unnamed colours where the tracer stacks them: after the named ones, in its automatic order', async () => {
      palettes['trace-9'].push({ index: 2, name: 'Teal', hex: '#1A7F7A', rgb: [26, 127, 122], share_percent: 10 })
      automatic['trace-9'] = ['#1A7F7A', '#E15F2D', '#0F233C']
      try {
        const panel = await openNewestRun('Traced')
        // Automatic: Teal at the bottom, Navy on top (not palette order).
        await vi.waitFor(() => expect(listed(panel)).toEqual(['Navy', 'Orange', 'Teal']))
        // Naming only Navy puts it at the bottom; Teal then Orange follow as inferred.
        fireEvent.click(within(panel).getByRole('button', { name: 'Move Navy down a layer' }))
        fireEvent.click(within(panel).getByRole('button', { name: 'Move Navy down a layer' }))
        await vi.waitFor(() => expect(listed(panel)).toEqual(['Orange', 'Teal', 'Navy']))
        cleanup()
        localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { solid_underlayers: true, layer_order: ['#0F233C'] } }))
        const reopened = await openNewestRun('Traced')
        await vi.waitFor(() => expect(listed(reopened)).toEqual(['Orange', 'Teal', 'Navy']))
      } finally { palettes['trace-9'].pop() }
    })

    it('says it is loading, never that there is no flatten, while a flattened revision loads', async () => {
      mocks.effectiveArtifacts.mockImplementation(async (id: string) => {
        if (id === 'revision-1') return [artifact('source')]
        if (id === 'trace-9') await new Promise<void>(resolve => { release = resolve })
        return run(id)
      })
      localStorage.setItem(`vinylgen.project-settings.${project.id}`, JSON.stringify({ trace: { solid_underlayers: true } }))
      window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=revision-1`)
      render(createElement((await import('./App')).default))
      const panel = screen.getByRole('tabpanel', { name: 'Trace' })
      await vi.waitFor(() => expect(panel.textContent).toContain('Flatten this revision to choose a layer order.'))
      act(() => {
        window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-9`)
        window.dispatchEvent(new HashChangeEvent('hashchange'))
      })
      await vi.waitFor(() => expect(release).toBeDefined())
      expect(panel.textContent).not.toContain('Flatten this revision')
      expect(panel.textContent).toContain('Loading the selected revision…')
      await act(async () => release!())
      await vi.waitFor(() => expect(listed(panel)).toEqual(['Orange', 'Navy']))
      expect(panel.textContent).not.toContain('Flatten this revision')
    })

    it('starts one job per button however often it is clicked while the start is pending', async () => {
      let answer: ((job: Job) => void) | undefined
      mocks.startPipeline.mockImplementation(() => new Promise<Job>(resolve => { answer = resolve }))
      const panel = await openNewestRun('Traced')
      const run = within(panel).getByRole('button', { name: 'Trace and validate' }) as HTMLButtonElement
      await vi.waitFor(() => expect(run.disabled).toBe(false))
      fireEvent.click(run)
      fireEvent.click(run)
      const flatten = within(screen.getByRole('tabpanel', { name: 'Flatten' })).getByRole('button', { name: 'Flatten image' }) as HTMLButtonElement
      fireEvent.click(flatten)
      const cleanupButton = within(screen.getByRole('tabpanel', { name: 'Cleanup' })).getByRole('button', { name: 'Run cleanup' }) as HTMLButtonElement
      fireEvent.click(cleanupButton)
      expect(mocks.startPipeline).toHaveBeenCalledTimes(1)
      expect([run.disabled, flatten.disabled, cleanupButton.disabled]).toEqual([true, true, true])
      await act(async () => answer!({ ...queuedJob, kind: 'trace' }))
      expect(mocks.startPipeline).toHaveBeenCalledTimes(1)
    })
  })
})

describe('Flatten canvas preservation markers and overlays', () => {
  const paletteRevision: Revision = { ...revision, id: 'palette-1', parent_revision_id: 'revision-1', kind: 'palette', settings: { palette: {} } }
  const overlays = [artifact('protected-areas-overlay'), artifact('trace-fallbacks-overlay'), artifact('trace-missed-details-overlay')]
  const TRACE_KINDS = ['trace-missed-details-overlay', 'trace-fallbacks-overlay']

  async function openFlatten() {
    mocks.projects.mockResolvedValue([project])
    mocks.project.mockResolvedValue({ ...project, revisions: [paletteRevision, revision] })
    mocks.effectiveArtifacts.mockResolvedValue([artifact('source'), artifact('flattened'), ...overlays])
    window.history.replaceState(null, '', `/#stage=flatten&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(workspaceProps.current?.canvasLabel).toBe('Flatten canvas'))
    await vi.waitFor(() => expect(workspaceProps.current?.layers?.some(layer => layer.id === 'protected-areas-overlay-id')).toBe(true))
  }

  it('offers each canvas only its own overlays: Protected areas on Flatten, the trace audits on Trace, the cleanup changes on Cleanup', async () => {
    await openFlatten()
    expect(workspaceProps.current?.layers?.find(layer => layer.id === 'protected-areas-overlay-id')).toMatchObject({ role: 'overlay', name: 'Protected areas' })
    expect(workspaceProps.current?.inspectionKinds).toEqual(['protected-areas-overlay'])
    fireEvent.click(screen.getByRole('tab', { name: 'Trace' }))
    await vi.waitFor(() => expect(workspaceProps.current?.canvasLabel).toBe('Trace canvas'))
    expect(workspaceProps.current?.inspectionKinds).toEqual(TRACE_KINDS)
    fireEvent.click(screen.getByRole('tab', { name: 'Cleanup' }))
    await vi.waitFor(() => expect(workspaceProps.current?.canvasLabel).toBe('Cleanup canvas'))
    expect(workspaceProps.current?.inspectionKinds).toEqual(['cleanup-changes-overlay'])
    expect(canvasOverlayKinds('flatten', true)).toEqual(['protected-areas-overlay'])
    expect(canvasOverlayKinds('trace', true)).toEqual([])
    expect(canvasOverlayKinds('cleanup', true)).toEqual([])
    expect(canvasOverlayKinds('palette', false)).toEqual([])
  })

  it('marks every chosen point on the canvas on the source grid, with a pending pair and a row highlight', async () => {
    await openFlatten()
    expect(workspaceProps.current?.markers).toEqual([])
    expect(workspaceProps.current?.markerGridFrom).toBe('source-id')
    const panel = screen.getByRole('tabpanel', { name: 'Flatten' })
    const pick = async (button: RegExp, point: [number, number]) => {
      fireEvent.click(within(panel).getByRole('button', { name: button }))
      await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeTypeOf('function'))
      expect(workspaceProps.current?.pickAim).toBe(true)
      act(() => workspaceProps.current!.onSelectPoint!(point))
    }
    await pick(/Protect connected detail/, [12, 34])
    await pick(/Preserve colour family/, [5, 6])
    await pick(/Keep two colours separate/, [1, 2])
    // The first pick of a pair shows as a pending marker numbered as its future row.
    expect(workspaceProps.current?.markers).toContainEqual({ id: 'pair-pending', kind: 'pair', label: '1', points: [[1, 2]], pending: true })
    act(() => workspaceProps.current!.onSelectPoint!([3, 4]))
    // Out of pick mode the markers stay on the Flatten canvas.
    await vi.waitFor(() => expect(workspaceProps.current?.onSelectPoint).toBeUndefined())
    expect(workspaceProps.current?.markers).toEqual([
      { id: 'protect-0', kind: 'protect', label: '1', points: [[12, 34]] },
      { id: 'preserve-0', kind: 'preserve', label: '1', points: [[5, 6]] },
      { id: 'pair-0', kind: 'pair', label: '1', points: [[1, 2], [3, 4]] },
    ])
    const row = within(panel).getByText('12, 34').closest('.point-row') as HTMLElement
    expect(row.querySelector('.point-number')?.textContent).toBe('1')
    fireEvent.mouseEnter(row)
    expect(workspaceProps.current?.highlightedMarker).toBe('protect-0')
    fireEvent.mouseLeave(row)
    expect(workspaceProps.current?.highlightedMarker).toBeNull()
    const pairRow = within(panel).getByText('1, 2 ↔ 3, 4').closest('.point-row') as HTMLElement
    fireEvent.focus(within(pairRow).getByRole('button', { name: 'Remove' }))
    expect(workspaceProps.current?.highlightedMarker).toBe('pair-0')
    // Removing the highlighted row clears the highlight with it.
    fireEvent.click(within(pairRow).getByRole('button', { name: 'Remove' }))
    expect(workspaceProps.current?.highlightedMarker).toBeNull()
    expect(workspaceProps.current?.markers?.map(marker => marker.id)).toEqual(['protect-0', 'preserve-0'])
    expect(workspaceProps.current?.pickAim).toBe(false)
    // A highlight never outlives the lists it was made against: after a new pick (the row
    // still hovered, with no leave event) no marker is highlighted.
    fireEvent.mouseEnter(row)
    expect(workspaceProps.current?.highlightedMarker).toBe('protect-0')
    await pick(/Protect connected detail/, [7, 8])
    expect(workspaceProps.current?.highlightedMarker).toBeNull()
  })
})
