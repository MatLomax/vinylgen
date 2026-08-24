import { FormEvent, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Download, Layers3, Palette, Pencil, Sparkles, Upload, WandSparkles } from 'lucide-react'
import { api, followJob, type Artifact, type InkscapeHandoff, type Job, type PaletteColour, type PaletteSettings, type Project, type Revision, type TraceSettings } from './api'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'
import { ArtworkPreview } from './ArtworkPreview'
import { FlattenedEditor } from './FlattenedEditor'
import { RegionEditor } from './RegionEditor'
import { neighbourLabels, overlayPixels, selectionLabels, toggleComponent, type Component, type Pixels } from './regionSelection'
import { DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS, PipelineControls, type SelectionMode } from './PipelineControls'
import './styles.css'

const MASK_ARTIFACT_KINDS = new Set(['edit-mask'])
const OVERLAY_ARTIFACT_KINDS = new Set([
  'trace-fallbacks',
  'trace-semantic-warnings',
])

type Dimensions = [number, number]
type WorkflowStage = 'create' | 'upload' | 'flatten' | 'trace' | 'palette' | 'edit' | 'export'
type ArtworkView = 'source' | 'flattened' | 'edited' | 'traced'

const WORKFLOW_STAGES: { id: WorkflowStage; label: string; description: string; icon: typeof Sparkles }[] = [
  { id: 'create', label: 'Create', description: 'Generate a source', icon: Sparkles },
  { id: 'upload', label: 'Upload', description: 'Import an image', icon: Upload },
  { id: 'flatten', label: 'Flatten', description: 'Reduce the palette', icon: WandSparkles },
  { id: 'palette', label: 'Palette', description: 'Edit whole colours', icon: Palette },
  { id: 'edit', label: 'Edit', description: 'Edit selected areas', icon: Pencil },
  { id: 'trace', label: 'Trace', description: 'Build vinyl layers', icon: Layers3 },
  { id: 'export', label: 'Export', description: 'Download or hand off', icon: Download },
]

export function stageFromHash(hash: string): WorkflowStage {
  const requested = new URLSearchParams(hash.replace(/^#/, '')).get('stage')
  return WORKFLOW_STAGES.some(stage => stage.id === requested) ? requested as WorkflowStage : 'create'
}

function hashParameters(): URLSearchParams {
  return new URLSearchParams(window.location.hash.replace(/^#/, ''))
}

function greatestCommonDivisor(first: number, second: number): number {
  while (second) [first, second] = [second, first % second]
  return first
}

export function customRatioDimensions(width: number, height: number): Dimensions | null {
  if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width <= 0 || height <= 0) return null
  const divisor = greatestCommonDivisor(width, height)
  const ratioWidth = width / divisor
  const ratioHeight = height / divisor
  const ratio = ratioWidth / ratioHeight
  const [maximumWidth, maximumHeight] = ratio > 1.15 ? [1536, 1024]
    : ratio < 0.87 ? [1024, 1536] : [1024, 1024]
  const scale = Math.floor(Math.min(maximumWidth / ratioWidth, maximumHeight / ratioHeight))
  const dimensions: Dimensions = [ratioWidth * scale, ratioHeight * scale]
  return scale > 0 && dimensions.every(value => value >= 256) ? dimensions : null
}

export function artifactToWorkspaceLayer(artifact: Artifact): WorkspaceLayer | null {
  if (!artifact.media_type.startsWith('image/')) return null
  const role = MASK_ARTIFACT_KINDS.has(artifact.kind) ? 'mask'
    : OVERLAY_ARTIFACT_KINDS.has(artifact.kind) ? 'overlay'
      : 'image'
  return {
    id: artifact.id,
    name: `${artifact.kind} · ${artifact.filename}`,
    url: artifact.download_url,
    mediaType: artifact.media_type,
    role,
  }
}

const draftKey = (projectId: string) => `vinylgen.project-settings.${projectId}`

type GenerationRecord = { short_prompt: string; width: number; height: number; desired_colours: number; required_motifs: string[]; rejected_motifs: string[] }

export function generationFromRevision(revision: Revision | undefined): GenerationRecord | null {
  const settings = revision?.settings
  if (!settings || typeof settings.short_prompt !== 'string') return null
  return {
    short_prompt: settings.short_prompt,
    width: Number(settings.width), height: Number(settings.height),
    desired_colours: Number(settings.desired_colours),
    required_motifs: Array.isArray(settings.required_motifs) ? settings.required_motifs as string[] : [],
    rejected_motifs: Array.isArray(settings.rejected_motifs) ? settings.rejected_motifs as string[] : [],
  }
}

export function frameFromDimensions(width: number, height: number): string {
  if (width === 1536 && height === 1024) return 'landscape'
  if (width === 1024 && height === 1536) return 'portrait'
  if (width === 1024 && height === 1024) return 'square'
  return 'custom'
}

export function inheritedPipelineSettings(revisions: Revision[], revisionId: string | null): { palette?: Partial<PaletteSettings>; trace?: Partial<TraceSettings> } {
  let current = revisions.find(row => row.id === revisionId)
  let palette: Partial<PaletteSettings> | undefined
  let trace: Partial<TraceSettings> | undefined
  const seen = new Set<string>()
  while (current && (!palette || !trace) && !seen.has(current.id)) {
    seen.add(current.id)
    if (!palette && current.settings.palette) palette = current.settings.palette as Partial<PaletteSettings>
    if (!trace && current.settings.trace) trace = current.settings.trace as Partial<TraceSettings>
    current = revisions.find(row => row.id === current?.parent_revision_id)
  }
  return { palette, trace }
}

export default function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [selected, setSelected] = useState<string | null>(() => hashParameters().get('project'))
  const [job, setJob] = useState<Job | null>(null)
  const [revision, setRevision] = useState<string | null>(() => hashParameters().get('revision'))
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [revisions, setRevisions] = useState<Revision[]>([])
  const [error, setError] = useState('')
  const [handoff, setHandoff] = useState<InkscapeHandoff | null>(null)
  const [handoffMessage, setHandoffMessage] = useState('')
  const [frame, setFrame] = useState('landscape')
  const [customRatio, setCustomRatio] = useState<Dimensions>([16, 9])
  const [stage, setStage] = useState<WorkflowStage>(() => stageFromHash(window.location.hash))
  const [view, setView] = useState<ArtworkView>('source')
  const [preEditFlattened, setPreEditFlattened] = useState<Artifact | null>(null)
  const [projectLoading, setProjectLoading] = useState(false)
  const [paletteSettings, setPaletteSettings] = useState<PaletteSettings>(DEFAULT_PALETTE_SETTINGS)
  const [traceSettings, setTraceSettings] = useState<TraceSettings>(DEFAULT_TRACE_SETTINGS)
  const [selectionMode, setSelectionMode] = useState<SelectionMode>(null)
  const [distinctFirst, setDistinctFirst] = useState<[number, number] | null>(null)
  const [regionSelection, setRegionSelection] = useState<Component[]>([])
  const [flattenedPixels, setFlattenedPixels] = useState<Pixels | null>(null)
  const [editorPalette, setEditorPalette] = useState<PaletteColour[]>([])
  const [overlayUrl, setOverlayUrl] = useState<string | undefined>(undefined)
  const [regionBusy, setRegionBusy] = useState(false)
  const [regionError, setRegionError] = useState('')
  const [desktopWorkflow, setDesktopWorkflow] = useState(() => window.innerWidth > 700)
  const stageScroll = useRef<Partial<Record<WorkflowStage, number>>>({})
  const loadedProject = useRef<string | null>(null)
  const current = revisions.find(row => row.id === revision)
  const flattened = artifacts.find(row => row.kind === 'flattened')
  const tracedPreview = artifacts.find(row => row.kind === 'traced-preview')
  const { hasFlatten, hasEdit, paletteAncestorId } = useMemo(() => {
    let node = revisions.find(row => row.id === revision)
    const seen = new Set<string>()
    let flatten = false, edit = false, paletteId: string | null = null
    while (node && !seen.has(node.id)) {
      seen.add(node.id)
      if (node.kind === 'palette') { flatten = true; if (!paletteId) paletteId = node.id }
      if (node.kind === 'label-edit') edit = true
      const parent = node.parent_revision_id
      node = parent ? revisions.find(row => row.id === parent) : undefined
    }
    return { hasFlatten: flatten, hasEdit: edit, paletteAncestorId: paletteId }
  }, [revision, revisions])
  const source = artifacts.find(row => row.kind === 'source')
  const flattenAvailable = hasFlatten || Boolean(flattened)
  const editableView: ArtworkView = hasEdit ? 'edited' : 'flattened'
  const defaultView: ArtworkView = hasEdit ? 'edited' : flattenAvailable ? 'flattened' : 'source'
  const viewOptions: { id: ArtworkView; label: string; available: boolean }[] = [
    { id: 'source', label: 'Source', available: Boolean(source) },
    { id: 'flattened', label: 'Flattened', available: flattenAvailable },
    { id: 'edited', label: 'Edited', available: hasEdit },
    { id: 'traced', label: 'Traced', available: Boolean(tracedPreview) },
  ]
  const availableViews = viewOptions.filter(option => option.available).map(option => option.id)
  const editSelectable = view === editableView
  const editableLabel = hasEdit ? 'Edited' : 'Flattened'
  // The baseline is the settings that produced the shown image (recorded on the
  // nearest ancestor revision), falling back to defaults before the step has run.
  // Fields diverging from it are flagged as pending edits.
  const pipelineBaseline = useMemo(() => inheritedPipelineSettings(revisions, current?.id || null), [revisions, current?.id])
  const baselinePalette = useMemo<PaletteSettings>(() => ({ ...DEFAULT_PALETTE_SETTINGS, ...pipelineBaseline.palette }), [pipelineBaseline])
  const baselineTrace = useMemo<TraceSettings>(() => ({ ...DEFAULT_TRACE_SETTINGS, ...pipelineBaseline.trace }), [pipelineBaseline])
  const sourceRevision = revisions.find(row => row.id === source?.revision_id)
  const generation = generationFromRevision(sourceRevision)

  useEffect(() => { api.projects().then(setProjects).catch(e => setError(String(e))) }, [])
  useEffect(() => {
    const syncWorkflowOrientation = () => setDesktopWorkflow(window.innerWidth > 700)
    window.addEventListener('resize', syncWorkflowOrientation)
    return () => window.removeEventListener('resize', syncWorkflowOrientation)
  }, [])
  useEffect(() => job ? followJob(job.id, next => {
    setError('')
    setJob(next)
    if (next.status === 'succeeded' && next.result_revision_id) {
      // The run recorded these values on the new revision; that revision is now the
      // baseline, so the pending draft for this step is spent — drop it before the
      // settings effect reloads (baseline alone then equals the applied values).
      if ((next.kind === 'palette' || next.kind === 'trace') && next.project_id) clearDraft(next.project_id, next.kind)
      setRevision(next.result_revision_id)
    }
  }, () => setError('Lost the live job connection')) : undefined, [job?.id])
  useEffect(() => {
    if (!selected) { setArtifacts([]); setRevision(null); setRevisions([]); loadedProject.current = null; return }
    const controller = new AbortController()
    // Only blank the canvas when switching projects; an edit that swaps revisions
    // within the same project keeps its artwork (and its zoom/pan) until the new
    // artifacts arrive.
    const switchingProject = loadedProject.current !== selected
    if (switchingProject) { setProjectLoading(true); setArtifacts([]) }
    api.project(selected, controller.signal).then(async project => {
      setRevisions(project.revisions)
      const latest = revision && project.revisions.some(row => row.id === revision)
        ? revision : project.revisions[0]?.id
      setRevision(latest || null)
      setArtifacts(latest ? await api.effectiveArtifacts(latest, controller.signal) : [])
      loadedProject.current = selected
    }).catch(cause => {
      if (!controller.signal.aborted) setError(String(cause))
    }).finally(() => { if (!controller.signal.aborted) setProjectLoading(false) })
    return () => controller.abort()
  }, [selected, revision])
  useEffect(() => {
    const syncStage = () => {
      stageScroll.current[stage] = window.scrollY
      const parameters = hashParameters()
      const next = stageFromHash(window.location.hash)
      setStage(next)
      if (next !== 'flatten' && next !== 'trace') { setSelectionMode(null); setDistinctFirst(null) }
      setSelected(parameters.get('project'))
      setRevision(parameters.get('revision'))
      requestAnimationFrame(() => window.scrollTo?.({ top: stageScroll.current[next] || 0 }))
      requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(`[data-stage-button="${next}"]`)?.focus())
    }
    window.addEventListener('hashchange', syncStage)
    window.addEventListener('popstate', syncStage)
    return () => {
      window.removeEventListener('hashchange', syncStage)
      window.removeEventListener('popstate', syncStage)
    }
  }, [stage])
  useEffect(() => {
    const parameters = hashParameters()
    parameters.set('stage', stage)
    if (selected) parameters.set('project', selected); else parameters.delete('project')
    if (revision) parameters.set('revision', revision); else parameters.delete('revision')
    const nextHash = parameters.toString()
    if (window.location.hash !== `#${nextHash}`) window.history.replaceState(null, '', `#${nextHash}`)
  }, [stage, selected, revision])
  useEffect(() => {
    setHandoff(null); setHandoffMessage('')
    if (revision && artifacts.some(row => row.media_type === 'image/svg+xml')) {
      const controller = new AbortController()
      api.inkscapeHandoff(revision, controller.signal).then(setHandoff).catch(cause => { if (!controller.signal.aborted) setError(String(cause)) })
      return () => controller.abort()
    }
  }, [revision, artifacts.map(row => row.id).join(',')])
  useEffect(() => {
    // Fill each stage's fields from the settings that produced the shown revision
    // (defaults before it ran), then overlay any pending per-step draft so un-run
    // edits survive a refresh until the stage is run.
    const { palette: storedPalette, trace: storedTrace } = inheritedPipelineSettings(revisions, current?.id || null)
    let draft: { palette?: Partial<PaletteSettings>; trace?: Partial<TraceSettings> } = {}
    try { draft = selected ? JSON.parse(localStorage.getItem(draftKey(selected)) || '{}') : {} } catch { /* use revision values */ }
    setPaletteSettings({ ...DEFAULT_PALETTE_SETTINGS, ...storedPalette, ...draft.palette })
    setTraceSettings({ ...DEFAULT_TRACE_SETTINGS, ...storedTrace, ...draft.trace })
    setSelectionMode(null); setDistinctFirst(null)
  }, [current?.id, selected])
  useEffect(() => {
    // Point the Create frame controls at whatever generated the shown source; text
    // fields fill via the form's keyed defaultValues, but frame/ratio are state.
    // With no generation on the shown source (uploaded, or nothing yet), fall back
    // to the default frame so the Create form reads as a clean slate.
    if (!generation || !Number.isFinite(generation.width) || !Number.isFinite(generation.height)) { setFrame('landscape'); return }
    setFrame(frameFromDimensions(generation.width, generation.height))
    if (frameFromDimensions(generation.width, generation.height) === 'custom') {
      const divisor = greatestCommonDivisor(generation.width, generation.height) || 1
      setCustomRatio([generation.width / divisor, generation.height / divisor])
    }
  }, [sourceRevision?.id])
  useEffect(() => {
    setRegionSelection([]); setRegionError('')
    if (!revision || !flattened) { setFlattenedPixels(null); setEditorPalette([]); return }
    const controller = new AbortController()
    api.editor(revision, controller.signal).then(state => setEditorPalette(state.palette))
      .catch(cause => { if (!controller.signal.aborted) setRegionError(String(cause)) })
    const image = new Image()
    image.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = image.naturalWidth; canvas.height = image.naturalHeight
      const context = canvas.getContext('2d')
      if (!context) return
      context.drawImage(image, 0, 0)
      try {
        const data = context.getImageData(0, 0, canvas.width, canvas.height)
        setFlattenedPixels({ data: data.data, width: canvas.width, height: canvas.height })
      } catch { /* canvas unavailable; region selection stays disabled */ }
    }
    image.src = flattened.download_url
    return () => { controller.abort(); image.onload = null }
  }, [revision, flattened?.id])
  useEffect(() => {
    if (!flattenedPixels || regionSelection.length === 0) { setOverlayUrl(undefined); return }
    const { width, height } = flattenedPixels
    const canvas = document.createElement('canvas')
    canvas.width = width; canvas.height = height
    const context = canvas.getContext('2d')
    if (!context) { setOverlayUrl(undefined); return }
    const image = context.createImageData(width, height)
    image.data.set(overlayPixels(regionSelection, width, height))
    context.putImageData(image, 0, 0)
    setOverlayUrl(canvas.toDataURL())
  }, [regionSelection, flattenedPixels])

  useEffect(() => {
    if (availableViews.length && !availableViews.includes(view)) setView(defaultView)
  }, [availableViews.join(','), view, defaultView])
  useEffect(() => {
    if (stage === 'palette' || stage === 'edit') setView(editableView)
  }, [stage, editableView])
  useEffect(() => {
    if (selectionMode) setView('source')
  }, [selectionMode])
  useEffect(() => {
    setPreEditFlattened(null)
    if (!hasEdit || !paletteAncestorId) return
    const controller = new AbortController()
    api.artifacts(paletteAncestorId, controller.signal)
      .then(rows => { const original = rows.find(row => row.kind === 'flattened'); if (original) setPreEditFlattened(original) })
      .catch(() => { /* leave null; the Flattened view falls back to the current flattened */ })
    return () => controller.abort()
  }, [hasEdit, paletteAncestorId])

  async function importImage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    const form = new FormData(event.currentTarget)
    const file = form.get('source') as File
    if (!file?.size) return setError('Choose an image to import')
    try {
      const project = await api.importProject(String(form.get('name') || file.name.replace(/\.[^.]+$/, '')), file)
      setProjects(current => [project, ...current])
      setSelected(project.id)
      setRevision(project.revisions[0].id)
      event.currentTarget.reset()
    } catch (cause) { setError(String(cause)) }
  }

  async function generateImage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    const form = new FormData(event.currentTarget)
    const prompt = String(form.get('prompt') || '').trim()
    if (!prompt) return setError('Enter a subject to generate')
    const ratio = String(form.get('ratio'))
    const dimensions: Dimensions | null = ratio === 'portrait' ? [1024, 1536] : ratio === 'square' ? [1024, 1024]
      : ratio === 'custom' ? customRatioDimensions(Number(form.get('ratio_width')), Number(form.get('ratio_height'))) : [1536, 1024]
    if (!dimensions) return setError('Choose a whole-number ratio between 1:6 and 6:1')
    const list = (name: string) => String(form.get(name) || '').split(',').map(value => value.trim()).filter(Boolean)
    try {
      const started = await api.generate(prompt, String(form.get('project_name') || ''), {
        width: dimensions[0], height: dimensions[1],
        desired_colours: Number(form.get('desired_colours')),
        required_motifs: list('required'), rejected_motifs: list('rejected'),
      })
      setProjects(current => [started.project, ...current])
      setSelected(started.project.id)
      setJob(started.job)
    } catch (cause) { setError(String(cause)) }
  }

  async function runPipeline(kind: 'palette' | 'trace' | 'validate' | 'export') {
    if (!selected || !revision) return
    setError('')
    try {
      const settings = kind === 'palette' ? { palette: paletteSettings }
        : kind === 'trace' ? { trace: traceSettings } : undefined
      if (settings) localStorage.setItem('vinylgen.pipeline-settings', JSON.stringify({ palette: paletteSettings, trace: traceSettings }))
      setJob(await api.startPipeline(kind, selected, revision, settings))
    }
    catch (cause) { setError(String(cause)) }
  }
  function persistDraft(step: 'palette' | 'trace') {
    if (!selected) return
    let draft: { palette?: PaletteSettings; trace?: TraceSettings } = {}
    try { draft = JSON.parse(localStorage.getItem(draftKey(selected)) || '{}') } catch { draft = {} }
    if (step === 'palette') draft.palette = paletteSettings; else draft.trace = traceSettings
    localStorage.setItem(draftKey(selected), JSON.stringify(draft))
  }
  function clearDraft(projectId: string, step: 'palette' | 'trace') {
    let draft: { palette?: PaletteSettings; trace?: TraceSettings } = {}
    try { draft = JSON.parse(localStorage.getItem(draftKey(projectId)) || '{}') } catch { return }
    delete draft[step]
    localStorage.setItem(draftKey(projectId), JSON.stringify(draft))
  }
  async function reimportSvg(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selected || !revision) return
    const form = new FormData(event.currentTarget)
    const file = form.get('svg') as File
    if (!file?.size) return setError('Choose an edited SVG to reimport')
    try {
      const next = await api.reimportInkscape(selected, revision, file)
      setRevision(next.id); setHandoffMessage('Edited SVG validated and saved as a new revision.')
      event.currentTarget.reset()
    } catch (cause) { setError(String(cause)) }
  }
  const redo = revisions.filter(row => row.parent_revision_id === revision).sort((a, b) => a.created_at.localeCompare(b.created_at)).at(-1)
  const jobMessage = job?.status === 'failed'
    ? job.error || job.message || 'Job failed'
    : job?.message || job?.status
  const customDimensions = customRatioDimensions(...customRatio)
  const busy = ['queued', 'running'].includes(job?.status || '')
  // Show a filled, determinate bar only once a running job reports real progress;
  // before that (queued, or the first tick still pending) the bar stays indeterminate.
  const jobProgress = job?.status === 'running' && job.progress > 0 ? job.progress : null
  const workspaceLayers = artifacts.map(artifactToWorkspaceLayer).filter((layer): layer is WorkspaceLayer => layer !== null)
  const preEditLayer: WorkspaceLayer | null = hasEdit && preEditFlattened ? {
    id: preEditFlattened.id, name: `flattened (original) · ${preEditFlattened.filename}`,
    url: preEditFlattened.download_url, mediaType: preEditFlattened.media_type, role: 'image',
  } : null
  const displayLayers = preEditLayer ? [...workspaceLayers, preEditLayer] : workspaceLayers
  const viewLayerId = (target: ArtworkView): string | undefined =>
    target === 'source' ? source?.id
      : target === 'traced' ? tracedPreview?.id
        : target === 'edited' ? flattened?.id
          : hasEdit ? (preEditLayer?.id ?? flattened?.id) : flattened?.id

  const regionLabels = selectionLabels(regionSelection)
  const regionNeighbours = useMemo(
    () => flattenedPixels && regionSelection.length ? neighbourLabels(regionSelection, flattenedPixels, editorPalette) : [],
    [regionSelection, flattenedPixels, editorPalette])

  function toggleRegion(point: [number, number]) {
    if (!flattenedPixels) return
    setRegionSelection(selection => toggleComponent(selection, flattenedPixels, point[0], point[1], editorPalette))
  }

  async function applyRegionColour(label: number) {
    if (!selected || !revision) return
    const operations = regionSelection
      .filter(component => component.label !== label)
      .map(component => ({ tool: 'reassign_region' as const, point: component.point, target_label: label }))
    if (operations.length === 0) return
    setRegionBusy(true); setRegionError('')
    try {
      const next = await api.applyEdits(selected, revision, operations)
      setRegionSelection([])
      setRevision(next.id)
    } catch (cause) { setRegionError(String(cause)) }
    finally { setRegionBusy(false) }
  }

  function undoRegion() {
    if (current?.parent_revision_id) setRevision(current.parent_revision_id)
  }

  function selectPreservationPoint(point: [number, number]) {
    if (selectionMode === 'protect-component') {
      setPaletteSettings(value => ({ ...value, protected_component_points: [...value.protected_component_points, point] }))
      setSelectionMode(null)
    } else if (selectionMode === 'preserve-colour') {
      setPaletteSettings(value => ({ ...value, preserved_colour_points: [...value.preserved_colour_points, point] }))
      setSelectionMode(null)
    } else if (selectionMode === 'distinct-first') {
      setDistinctFirst(point); setSelectionMode('distinct-second')
    } else if (selectionMode === 'distinct-second' && distinctFirst) {
      setPaletteSettings(value => ({ ...value, distinct_colour_point_pairs: [...value.distinct_colour_point_pairs, [distinctFirst, point]] }))
      setDistinctFirst(null); setSelectionMode(null)
    }
  }

  function restoreLastSettings() {
    try {
      const stored = JSON.parse(localStorage.getItem('vinylgen.pipeline-settings') || '{}')
      setPaletteSettings({ ...DEFAULT_PALETTE_SETTINGS, ...(stored.palette || {}) })
      setTraceSettings({ ...DEFAULT_TRACE_SETTINGS, ...(stored.trace || {}) })
    } catch { setError('Saved pipeline settings could not be read. Reset them and try again.') }
  }

  function navigateStage(next: WorkflowStage) {
    if (next === stage) return
    stageScroll.current[stage] = window.scrollY
    const parameters = hashParameters()
    parameters.set('stage', next)
    if (selected) parameters.set('project', selected); else parameters.delete('project')
    if (revision) parameters.set('revision', revision); else parameters.delete('revision')
    window.history.pushState(null, '', `#${parameters}`)
    setStage(next)
    if (next !== 'flatten' && next !== 'trace') { setSelectionMode(null); setDistinctFirst(null) }
    requestAnimationFrame(() => window.scrollTo?.({ top: stageScroll.current[next] || 0 }))
  }

  function navigateStageByKey(event: KeyboardEvent<HTMLButtonElement>, currentStage: WorkflowStage) {
    const currentIndex = WORKFLOW_STAGES.findIndex(item => item.id === currentStage)
    const nextIndex = event.key === 'Home' ? 0
        : event.key === 'End' ? WORKFLOW_STAGES.length - 1
        : event.key === 'ArrowRight' || event.key === 'ArrowDown' ? (currentIndex + 1) % WORKFLOW_STAGES.length
          : event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? (currentIndex - 1 + WORKFLOW_STAGES.length) % WORKFLOW_STAGES.length
            : -1
    if (nextIndex < 0) return
    event.preventDefault()
    const nextStage = WORKFLOW_STAGES[nextIndex].id
    navigateStage(nextStage)
    requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-stage-button="${nextStage}"]`)?.focus())
  }

  return <main>
    <header><div className="mobile-brand"><span className="mark" aria-hidden="true">V</span><h1>Vinylgen</h1></div>{availableViews.length > 1 && <div className="view-toggle" role="group" aria-label="Artwork view">{viewOptions.filter(option => option.available).map(option => <button key={option.id} type="button" className={view === option.id ? 'active' : ''} aria-pressed={view === option.id} onClick={() => setView(option.id)}>{option.label}</button>)}</div>}<label className="active-project"><span>Active project</span><select aria-label="Active project" value={selected ?? ''} onChange={event => setSelected(event.target.value || null)}><option value="">No project selected</option>{projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label></header>
    <div className="workflow-sidebar">
      <div className="sidebar-brand"><span className="mark" aria-hidden="true">V</span><div><h1>Vinylgen</h1></div></div>
      <nav className="workflow-nav" aria-label="Artwork workflow" role="tablist" aria-orientation={desktopWorkflow ? 'vertical' : 'horizontal'}>
        {WORKFLOW_STAGES.map(item => <button key={item.id} id={`workflow-tab-${item.id}`} type="button" role="tab" aria-label={item.label} data-stage-button={item.id} aria-selected={stage === item.id} aria-controls={`workflow-panel-${item.id}`} tabIndex={stage === item.id ? 0 : -1} onKeyDown={event => navigateStageByKey(event, item.id)} onClick={() => navigateStage(item.id)}><item.icon aria-hidden="true"/><span><strong>{item.label}</strong><small>{item.description}</small></span>{(item.id === 'flatten' || item.id === 'trace') && busy && <i className="stage-running" aria-label="Job running" />}</button>)}
      </nav>
    </div>
    <p className="visually-hidden" aria-live="polite" aria-atomic="true">{WORKFLOW_STAGES.find(item => item.id === stage)?.label} stage</p>
    <section className="mobile-context" aria-label="Current work"><strong>{WORKFLOW_STAGES.find(item => item.id === stage)?.label}</strong><span>{projects.find(project => project.id === selected)?.name || 'No project selected'}</span></section>
    {error && <p className="error global-error" role="alert">{error}</p>}
    {job && !busy && <p className={job.status === 'failed' ? 'error job-result' : 'success job-result'} role={job.status === 'failed' ? 'alert' : 'status'} aria-live={job.status === 'failed' ? 'assertive' : 'polite'} aria-atomic="true">{jobMessage}</p>}
    <section className={`workspace workflow-${stage}`}>
      <div className={`workflow-surface artwork-surface ${projectLoading || busy ? 'is-loading' : ''} ${['flatten', 'trace', 'palette', 'edit'].includes(stage) || selectionMode || busy || projectLoading ? 'mobile-stage-active' : ''}`} data-stage="artwork">
        {stage === 'palette' || stage === 'edit' || selectionMode ? <ImageWorkspace
          layers={displayLayers} editorMode
          preferredLayerId={viewLayerId(view)}
          selectionLabel={stage === 'edit' ? 'Flattened artwork area selector' : selectionMode ? 'Source image preservation-point selector' : undefined}
          onSelectPoint={stage === 'edit' ? (editSelectable ? toggleRegion : undefined) : (selectionMode && view === 'source' ? selectPreservationPoint : undefined)}
          overlay={stage === 'edit' && editSelectable ? overlayUrl : undefined}
        /> : <ArtworkPreview layers={displayLayers} preferredLayerId={viewLayerId(view)} />}
        {(projectLoading || busy) && <section className="canvas-loading">{jobProgress === null && <span className="spinner" aria-hidden="true"/>}<h2>{projectLoading ? 'Loading artwork' : jobMessage || 'Working'}</h2><div className={jobProgress === null ? 'progress indeterminate' : 'progress'} role="progressbar" aria-label="Job progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={jobProgress === null ? undefined : Math.round(jobProgress * 100)} style={jobProgress === null ? undefined : { '--progress': jobProgress } as React.CSSProperties}><i /></div>{jobProgress !== null && <p className="progress-percent" aria-hidden="true">{Math.round(jobProgress * 100)}%</p>}<p role="status" aria-live="polite" aria-atomic="true">{projectLoading ? 'Fetching the selected revision and its artifacts…' : `${jobMessage}. This can take a while; Vinylgen is still working.`}</p>{busy && <button className="secondary" onClick={() => api.cancelJob(job!.id).then(setJob).catch(cause => setError(String(cause)))}>Cancel job</button>}</section>}
      </div>
      <aside>
        <section id="workflow-panel-create" role="tabpanel" aria-labelledby="workflow-tab-create" className={`side-section create-section workflow-surface ${stage === 'create' ? 'mobile-stage-active' : ''}`} data-stage="create">
        <h2>Create artwork</h2>
        <form className="generation-form" key={sourceRevision?.id ?? 'new-source'} onSubmit={generateImage}>
          <label>Short subject<textarea name="prompt" placeholder="e.g. sky whale city" defaultValue={generation?.short_prompt ?? ''} required /></label>
          <label>Project name<input name="project_name" placeholder="Defaults to subject" /></label>
          <label>Frame<select name="ratio" value={frame} onChange={event => setFrame(event.target.value)}><option value="landscape">Landscape 3:2</option><option value="portrait">Portrait 2:3</option><option value="square">Square 1:1</option><option value="custom">Custom ratio</option></select><small>Sets the generated canvas shape and maximum output dimensions.</small></label>
          {frame === 'custom' && <div className="custom-ratio"><div className="form-pair"><label>Ratio width<input name="ratio_width" type="number" min="1" step="1" value={customRatio[0]} onChange={event => setCustomRatio(current => [Number(event.target.value), current[1]])} aria-describedby="custom-ratio-guidance" aria-invalid={!customDimensions} required /></label><label>Ratio height<input name="ratio_height" type="number" min="1" step="1" value={customRatio[1]} onChange={event => setCustomRatio(current => [current[0], Number(event.target.value)])} aria-describedby="custom-ratio-guidance" aria-invalid={!customDimensions} required /></label></div><p id="custom-ratio-guidance" role="status" aria-live="polite">{customDimensions ? `Maximum output: ${customDimensions.join(' × ')} px` : 'Use a whole-number ratio between 1:6 and 6:1.'}</p></div>}
          <label>Number of colours<input name="desired_colours" type="number" min="2" max="12" step="1" defaultValue={String(generation?.desired_colours ?? 6)} required /><small>The source will use this exact flat palette. Choose the number of vinyl colours you want the artwork designed around.</small></label>
          <label>Required motifs<input name="required" placeholder="comma separated" defaultValue={generation?.required_motifs.join(', ') ?? ''} /><small>Objects or ideas that must appear in the generated scene.</small></label>
          <label>Rejected motifs<input name="rejected" placeholder="comma separated" defaultValue={generation?.rejected_motifs.join(', ') ?? ''} /><small>Objects or ideas the generation prompt must explicitly avoid.</small></label>
          <button type="submit" disabled={busy}>Generate source</button>
        </form>
        </section>
        <section id="workflow-panel-upload" role="tabpanel" aria-labelledby="workflow-tab-upload" className={`side-section upload-section workflow-surface ${stage === 'upload' ? 'mobile-stage-active' : ''}`} data-stage="upload">
        <h2>Upload source</h2><p className="section-intro">Start a new project from an existing raster image. The original file is preserved unchanged.</p>
        <form onSubmit={importImage}>
          <label>Name<input name="name" placeholder="Optional project name" /><small>Defaults to the uploaded filename.</small></label>
          <label className="file">Source image<input name="source" type="file" accept="image/*" required /><small>Choose the raster that will become the immutable source revision.</small></label>
          <button type="submit">Import image</button>
        </form>
        </section>
        <section id="workflow-panel-flatten" role="tabpanel" aria-labelledby="workflow-tab-flatten" className={`side-section pipeline-section workflow-surface ${stage === 'flatten' ? 'mobile-stage-active' : ''}`} data-stage="flatten">
        <div className="section-heading"><h2>Flatten colours</h2><span>{revision ? 'Ready' : 'Choose a project'}</span></div><p className="section-intro">Turn the source into an exact limited-colour raster. Review preservation exceptions before running.</p>
        <div className="job">
          {!revision && <p className="stage-guidance">Choose an active project in the header first.</p>}
          <button disabled={!revision || busy} onClick={() => runPipeline('palette')}>Flatten image</button>
        </div>
        <PipelineControls stage="flatten" palette={paletteSettings} setPalette={setPaletteSettings} basePalette={baselinePalette} trace={traceSettings} setTrace={setTraceSettings} baseTrace={baselineTrace}
          selectionMode={selectionMode} setSelectionMode={setSelectionMode}
          onReset={() => { setPaletteSettings(DEFAULT_PALETTE_SETTINGS); setSelectionMode(null) }}
          onRestoreLast={restoreLastSettings} onCommitDraft={() => persistDraft('palette')} />
        </section>
        <section id="workflow-panel-trace" role="tabpanel" aria-labelledby="workflow-tab-trace" className={`side-section pipeline-section workflow-surface ${stage === 'trace' ? 'mobile-stage-active' : ''}`} data-stage="trace">
          <div className="section-heading"><h2>Trace vinyl layers</h2><span>{artifacts.some(row => row.kind === 'segmentation') ? 'Ready' : 'Flatten first'}</span></div><p className="section-intro">Convert flattened colour regions into shared-boundary SVG layers.</p>
          <div className="job"><button disabled={!revision || !artifacts.some(row => row.kind === 'segmentation') || busy} onClick={() => runPipeline('trace')}>Trace and validate</button></div>
          <PipelineControls stage="trace" palette={paletteSettings} setPalette={setPaletteSettings} basePalette={baselinePalette} trace={traceSettings} setTrace={setTraceSettings} baseTrace={baselineTrace} selectionMode={selectionMode} setSelectionMode={setSelectionMode} onReset={() => setTraceSettings(DEFAULT_TRACE_SETTINGS)} onRestoreLast={restoreLastSettings} onCommitDraft={() => persistDraft('trace')} />
        </section>
        <section id="workflow-panel-palette" role="tabpanel" aria-labelledby="workflow-tab-palette" className={`side-section edit-section workflow-surface ${stage === 'palette' ? 'mobile-stage-active' : ''}`} data-stage="palette">
          {(!selected || !revision || !source) && <><h2>Editing unavailable</h2><p className="stage-guidance">Create or choose artwork first.</p></>}
          {selected && revision && flattened && <>
            <nav className="history" aria-label="Revision history"><button disabled={!current?.parent_revision_id} onClick={() => current?.parent_revision_id && setRevision(current.parent_revision_id)}>Undo</button><select aria-label="Revision" value={revision} onChange={event => setRevision(event.target.value)}>{revisions.map(row => <option key={row.id} value={row.id}>{row.kind} · {new Date(row.created_at).toLocaleString()}</option>)}</select><button disabled={!redo} onClick={() => redo && setRevision(redo.id)}>Redo</button></nav>
            <FlattenedEditor projectId={selected} revisionId={revision} flattened={flattened} onCommit={setRevision} disabled={!editSelectable} disabledHint={`Switch to the ${editableLabel} image to edit its palette.`} />
          </>}
          {selected && revision && source && !flattened && <><h2>Palette unavailable</h2><p className="stage-guidance">Flatten this revision first to edit its palette.</p></>}
        </section>
        <section id="workflow-panel-edit" role="tabpanel" aria-labelledby="workflow-tab-edit" className={`side-section region-edit-section workflow-surface ${stage === 'edit' ? 'mobile-stage-active' : ''}`} data-stage="edit">
          {!selected || !revision || !flattened ? <><h2>Edit selected areas</h2><p className="stage-guidance">Flatten the active project before editing individual areas.</p></> : <RegionEditor palette={editorPalette} selectionLabels={regionLabels} neighbourLabels={regionNeighbours} onPick={applyRegionColour} onClear={() => setRegionSelection([])} onUndo={undoRegion} canUndo={Boolean(current?.parent_revision_id)} busy={regionBusy} error={regionError} disabled={!editSelectable} disabledHint={`Switch to the ${editableLabel} image to select and edit areas.`} />}
        </section>
      </aside>
    </section>
    <section id="workflow-panel-export" role="tabpanel" aria-labelledby="workflow-tab-export" className={`workflow-surface export-surface ${stage === 'export' ? 'mobile-stage-active' : ''}`} data-stage="export">
      {!handoff && <section className="mobile-stage-state"><h2>Export unavailable</h2><p>Complete Trace to create validated, exportable SVG layers.</p></section>}
      {selected && revision && handoff && <section className="inkscape-handoff"><h2>Export or finish in Inkscape</h2><p>{handoff.guidance}</p><div className="handoff-actions">{handoff.exports.map(item => <span key={item.id}><a className="button-link" href={item.download_url}>Download {item.filename}</a>{handoff.shared_folder_enabled && <button className="secondary" onClick={() => api.saveForInkscape(revision, item.id).then(saved => setHandoffMessage(`Saved ${saved.filename} to the shared folder.`)).catch(cause => setError(String(cause)))}>Save {item.media_type === 'image/png' ? 'PNG' : 'SVG'} to shared folder</button>}</span>)}</div><form onSubmit={reimportSvg}><label className="file">Edited SVG<input name="svg" type="file" accept="image/svg+xml,.svg" required /></label><button type="submit">Validate and reimport</button></form>{handoffMessage && <p className="success" role="status" aria-live="polite">{handoffMessage}</p>}</section>}
    </section>
  </main>
}
