import { FormEvent, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Download, Eraser, Layers3, Palette, Pencil, Sparkles, Trash2, Upload, WandSparkles } from 'lucide-react'
import { api, followJob, type Artifact, type CleanupSettings, type EditorState, type InkscapeHandoff, type Job, type PaletteColour, type PaletteSettings, type Project, type Revision, type TraceSettings } from './api'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'
import { CLEANUP_CHANGES_OVERLAY_KIND } from './CleanupPreview'
import { ArtworkPreview } from './ArtworkPreview'
import { FlattenedEditor } from './FlattenedEditor'
import { RegionEditor } from './RegionEditor'
import { PencilEditor } from './PencilEditor'
import { UpscalePanel } from './UpscalePanel'
import { ConfirmDialog } from './ConfirmDialog'
import { palettePointsForRevision } from './sourceGrid'
import { hexToRgb, neighbourLabels, overlayPixels, paintOverlayPixels, paletteHasDuplicateRgb, pixelsWithComponents, pixelsWithPaintedCells, recolourPreviewPixels, regionReloadDecision, removeComponents, selectionLabels, toggleComponent, type Component, type OverlayFrame, type Pixels } from './regionSelection'
import { DEFAULT_CLEANUP_SETTINGS, DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS, PipelineControls, isPixelRecord, layerOrderMatters, legacyCleanupSettings, normalizeTraceSettings, pixelRecordInUnits, physicalSize, preservationMarkers, resolvePaletteColour, type SelectionMode } from './PipelineControls'
import './styles.css'

const MASK_ARTIFACT_KINDS = new Set(['edit-mask'])
// Traced SVGs are drawn on the flattened pixel grid (their viewBox), whatever their physical size.
const TRACED_SVG_KINDS = new Set(['traced-overlap', 'traced-edge'])
// Transparent audit overlays, offered on the canvas of their stage: the trace audits on
// the traced grid, a flatten's protected areas on its flattened grid, and what a Cleanup
// run changed on its raster (drawn by the Cleanup canvas's "Show changes" switch).
const OVERLAY_ARTIFACT_LABELS: Record<string, string> = {
  'trace-missed-details-overlay': 'Missed source details (red)',
  'trace-fallbacks-overlay': 'Tightened boundaries',
  'protected-areas-overlay': 'Protected areas',
  [CLEANUP_CHANGES_OVERLAY_KIND]: 'Cleanup changes (removed red, widened green)',
}
const TRACE_OVERLAY_KINDS = ['trace-missed-details-overlay', 'trace-fallbacks-overlay']
const FLATTEN_OVERLAY_KINDS = ['protected-areas-overlay']
const CLEANUP_OVERLAY_KINDS = [CLEANUP_CHANGES_OVERLAY_KIND]
const NO_OVERLAY_KINDS: string[] = []
// Standalone audit images that paint the whole faded source: kept as files, never layers.
const OPAQUE_AUDIT_KINDS = new Set(['trace-fallbacks', 'trace-semantic-warnings'])

type Dimensions = [number, number]
type WorkflowStage = 'create' | 'upload' | 'flatten' | 'cleanup' | 'trace' | 'palette' | 'edit' | 'export'
type ArtworkView = 'source' | 'flattened' | 'edited' | 'cleaned' | 'traced'

const WORKFLOW_STAGES: { id: WorkflowStage; label: string; description: string; icon: typeof Sparkles }[] = [
  { id: 'create', label: 'Create', description: 'Generate a source', icon: Sparkles },
  { id: 'upload', label: 'Upload', description: 'Import an image', icon: Upload },
  { id: 'flatten', label: 'Flatten', description: 'Reduce the palette', icon: WandSparkles },
  { id: 'palette', label: 'Palette', description: 'Edit whole colours', icon: Palette },
  { id: 'edit', label: 'Edit', description: 'Edit selected areas', icon: Pencil },
  { id: 'cleanup', label: 'Cleanup', description: 'Size and cutter cleanup', icon: Eraser },
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

// The overlay kinds a stage's canvas offers: its own audits, and none while a preservation
// point is picked on another stage's canvas.
export function canvasOverlayKinds(stage: WorkflowStage, picking: boolean): string[] {
  return stage === 'flatten' ? FLATTEN_OVERLAY_KINDS : stage === 'trace' && !picking ? TRACE_OVERLAY_KINDS
    : stage === 'cleanup' && !picking ? CLEANUP_OVERLAY_KINDS : NO_OVERLAY_KINDS
}

// The Before images a stage's canvas compares the image it shows with, natural input
// first (the canvas drops the shown image and any not covering its extent). Upload,
// which always shows the source, compares an upscaled source with the source it was
// made from; Flatten compares a
// result with its source; Cleanup its cleaned raster with the raster before cleanup;
// Trace its traced SVG with the raster traced; Palette and Edit offer every image, the
// raster before the edits (else the source) first. None while a preservation point is
// picked, or while the canvas shows something other than its stage's result.
export function canvasCompareBefore(stage: WorkflowStage, view: ArtworkView, picking: boolean, images: { originalSource?: string; source?: string; preEdit?: string; beforeCleanup?: string; raster?: string; all: string[] }): string[] {
  const listed = (ids: (string | undefined)[]) => [...new Set(ids.filter((id): id is string => Boolean(id)))]
  if (picking) return []
  if (stage === 'upload') return listed([images.originalSource])
  if (stage === 'flatten') return view === 'source' ? [] : listed([images.source])
  if (stage === 'palette' || stage === 'edit') return listed([images.preEdit, images.source, ...images.all])
  if (stage === 'cleanup') return view === 'cleaned' ? listed([images.beforeCleanup]) : []
  if (stage === 'trace') return view === 'traced' ? listed([images.raster]) : []
  return []
}

export function artifactToWorkspaceLayer(artifact: Artifact): WorkspaceLayer | null {
  if (!artifact.media_type.startsWith('image/') || OPAQUE_AUDIT_KINDS.has(artifact.kind)) return null
  const overlayLabel = OVERLAY_ARTIFACT_LABELS[artifact.kind]
  const role = MASK_ARTIFACT_KINDS.has(artifact.kind) ? 'mask'
    : overlayLabel ? 'overlay'
      : 'image'
  return {
    id: artifact.id,
    name: overlayLabel ?? `${artifact.kind} · ${artifact.filename}`,
    url: artifact.download_url,
    mediaType: artifact.media_type,
    role,
    kind: artifact.kind,
  }
}

// A pixel grid recorded on a revision as [width, height], or null.
function recordedGrid(value: unknown): { width: number; height: number } | null {
  return Array.isArray(value) && value.length === 2 && value.every(side => Number.isInteger(side) && side > 0) ? { width: value[0], height: value[1] } : null
}
// Why Palette and Edit commit nothing while the canvas compares.
const COMPARE_EDIT_LOCK = 'Turn Compare off to edit'

const draftKey = (projectId: string) => `vinylgen.project-settings.${projectId}`
const formatMegabytes = (bytes: number) => `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`

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

// Each stage's form settings from the nearest ancestor that recorded them. A trace run
// recorded before the cleanup stage embedded the size, removed colour and cutter
// cleanup in its trace settings; those fill the Cleanup form, never the Trace form.
// Palette points recorded before a source upscale are mapped onto the shown source.
// `recordedRemoveColour` is the inherited cleanup's removed colour exactly as recorded
// (a name or hex), before `recordedCleanupSettings` names it by hex.
export function inheritedPipelineSettings(revisions: Revision[], revisionId: string | null): { palette?: Partial<PaletteSettings>; cleanup?: Partial<CleanupSettings>; trace?: Partial<TraceSettings>; recordedRemoveColour?: string | null } {
  let current = revisions.find(row => row.id === revisionId)
  let palette: Partial<PaletteSettings> | undefined
  let cleanup: Partial<CleanupSettings> | undefined
  let trace: Partial<TraceSettings> | undefined
  let recordedRemoveColour: string | null | undefined
  const seen = new Set<string>()
  while (current && (!palette || !cleanup || !trace) && !seen.has(current.id)) {
    seen.add(current.id)
    if (!palette && current.settings.palette) palette = palettePointsForRevision(current.settings.palette as Partial<PaletteSettings>, revisions, current.id, revisionId!)
    if (!cleanup && current.settings.cleanup) {
      cleanup = recordedCleanupSettings(current)
      recordedRemoveColour = (current.settings.cleanup as Partial<CleanupSettings>).remove_colour
    }
    if (current.settings.trace) {
      const stored = current.settings.trace as Record<string, unknown>
      if (!trace) trace = normalizeTraceSettings(stored as Partial<TraceSettings>)
      if (!cleanup) { cleanup = legacyCleanupSettings(stored); recordedRemoveColour = cleanup?.remove_colour }
    }
    current = revisions.find(row => row.id === current?.parent_revision_id)
  }
  return { palette, cleanup, trace, recordedRemoveColour }
}

// The nearest `cleanup` ancestor and whether it is stale: a revision in between (a
// flatten, an edit, a source upscale) marked cleanup stale. Mirrors the server's
// nearest_cleanup, which blocks a trace while that cleanup is stale.
export function nearestCleanup(revisions: Revision[], revisionId: string | null): { revision: Revision | null; stale: boolean } {
  const seen = new Set<string>()
  let stale = false
  for (let node = revisions.find(row => row.id === revisionId); node && !seen.has(node.id);
    node = revisions.find(row => row.id === node?.parent_revision_id)) {
    seen.add(node.id)
    if (node.kind === 'cleanup' && node.settings.cleanup) return { revision: node, stale }
    if ((node.settings.stale_concerns as string[] | undefined)?.includes('cleanup')) stale = true
  }
  return { revision: null, stale: false }
}
// The cleanup a trace of this revision uses: the nearest cleanup while it is not stale.
export function activeCleanupRevision(revisions: Revision[], revisionId: string | null): Revision | null {
  const { revision, stale } = nearestCleanup(revisions, revisionId)
  return stale ? null : revision
}

// Whether a trace run's overlap was in mm: a run recorded before the cleanup stage says
// so in its own settings; any other run used the size of the cleanup behind it.
export function traceRunIsPhysical(revisions: Revision[], runId: string | undefined): boolean {
  const run = revisions.find(row => row.id === runId)
  const legacy = legacyCleanupSettings((run?.settings.trace ?? {}) as Record<string, unknown>)
  if (legacy && ('width_mm' in legacy || 'height_mm' in legacy)) return Boolean(legacy.width_mm || legacy.height_mm)
  const cleanup = activeCleanupRevision(revisions, runId ?? null)?.settings.cleanup as Partial<CleanupSettings> | undefined
  return physicalSize(cleanup, null) !== null
}

// Every complete stage setting recorded along the shown revision's ancestry, newest
// to oldest, so the form can step back through past attempts without changing the
// shown image. Each entry is merged over defaults so older revisions that predate a
// field still yield a full settings object, its points on the shown source's grid.
export function ancestorPaletteIterations(revisions: Revision[], revisionId: string | null): PaletteSettings[] {
  const list: PaletteSettings[] = []
  const seen = new Set<string>()
  let current = revisions.find(row => row.id === revisionId)
  while (current && !seen.has(current.id)) {
    seen.add(current.id)
    if (current.settings.palette) list.push(palettePointsForRevision({ ...DEFAULT_PALETTE_SETTINGS, ...(current.settings.palette as Partial<PaletteSettings>) }, revisions, current.id, revisionId!))
    current = revisions.find(row => row.id === current?.parent_revision_id)
  }
  return list
}
// The cleanup settings recorded on each real `cleanup` revision along the shown
// revision's ancestry, newest first and merged over defaults, so the Cleanup form
// steps back through them like Flatten. A pre-cleanup-stage trace run's embedded
// values fill the form's baseline (see inheritedPipelineSettings) but are not a run
// of the Cleanup stage, so they are not counted as an iteration here.
// A cleanup revision's recorded settings as the Cleanup form takes them: the removed
// colour named by its hex in the palette that cleanup ran with (`removed_colour_hex`,
// pipeline.cleanup_removed_hex), not by the recorded name, which a trace's colour names
// may since have given to another colour. Without that hex (a record the server could
// not resolve) the recorded value is kept.
export function recordedCleanupSettings(revision: Revision): Partial<CleanupSettings> {
  const recorded = revision.settings.cleanup as Partial<CleanupSettings>
  const hex = revision.settings.removed_colour_hex
  return recorded.remove_colour != null && typeof hex === 'string' ? { ...recorded, remove_colour: hex } : recorded
}
// The editor state's inherited removed colour, as the state `revision` holds it.
// Unresolved, the form holds the cleanup's hex (`value` here), which blocks Cleanup until
// the user edits it (`unresolved`), rather than a blank that would remove nothing.
export type InheritedRemove = { revision: string; value: string | null; unresolved: boolean; problem: 'missing' | 'ambiguous' | null }
export function inheritedRemoveOf(revision: string, state: EditorState): InheritedRemove | null {
  const inherited = state.inherited_remove_colour
  return inherited ? { revision, value: inherited.unresolved ? inherited.hex ?? null : inherited.value, unresolved: inherited.unresolved, problem: inherited.problem ?? null } : null
}
// Why Cleanup is blocked on an unresolved inherited colour, saying what is true: gone
// from the palette (a re-flatten, recolour or merge; a name is never carried across a
// palette change) or matching more than one colour.
export function unresolvedRemoveReason(value: string, problem: InheritedRemove['problem']): string {
  return problem === 'ambiguous'
    ? `The colour the last Cleanup removed (${value}) now matches more than one colour of this artwork's palette, by hex or by name. Choose the colour to remove, or clear the field to remove none.`
    : `The colour the last Cleanup removed (${value}) is not in this artwork's palette any more (it was re-flattened, recoloured or merged). Choose the colour to remove, or clear the field to remove none.`
}
export function ancestorCleanupIterations(revisions: Revision[], revisionId: string | null): CleanupSettings[] {
  const list: CleanupSettings[] = []
  const seen = new Set<string>()
  let current = revisions.find(row => row.id === revisionId)
  while (current && !seen.has(current.id)) {
    seen.add(current.id)
    const recorded = current.kind === 'cleanup' && current.settings.cleanup ? recordedCleanupSettings(current) : undefined
    if (recorded) list.push({ ...DEFAULT_CLEANUP_SETTINGS, ...recorded })
    current = revisions.find(row => row.id === current?.parent_revision_id)
  }
  return list
}
// Whether the ancestry of `revisionId` holds a flatten and an area/pencil edit, and its
// nearest palette (flatten) revision, which wrote the original the edits started from.
export function editAncestry(revisions: Revision[], revisionId: string | null): { hasFlatten: boolean; hasEdit: boolean; paletteAncestorId: string | null } {
  let node = revisions.find(row => row.id === revisionId)
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
}
// The trace settings with `layer_order` narrowed to the colours of `palette` that get a
// layer (never the palette index `removedIndex`, which the active cleanup removes), matched as the tracer
// does (resolvePaletteColour), each once and in the stored order. Colours the order no
// longer names fall to the automatic order; an order naming none becomes null.
export function withPaletteLayerOrder(trace: TraceSettings, palette: PaletteColour[], removedIndex: number | null = null): TraceSettings {
  if (!trace.layer_order) return trace
  const matched: number[] = []
  for (const entry of trace.layer_order) {
    const colour = resolvePaletteColour(entry, palette)
    if (colour && colour.index !== removedIndex && !matched.includes(colour.index)) matched.push(colour.index)
  }
  const kept = matched.map(index => palette.find(row => row.index === index)!.hex)
  if (kept.length === trace.layer_order.length && kept.every((hex, at) => hex === trace.layer_order![at])) return trace
  return { ...trace, layer_order: kept.length ? kept : null }
}
// The trace settings a run or preview sends: without solid underlayers or overlap the
// stacking changes no geometry, so a stored layer order is left out (kept in the form) and
// the layers stack as the tracer's default.
export function withoutMootLayerOrder(trace: TraceSettings): TraceSettings {
  return trace.layer_order && !layerOrderMatters(trace) ? { ...trace, layer_order: null } : trace
}
// Whether two stored layer orders name the same colours in the same order.
function sameOrder(a: string[] | null | undefined, b: string[] | null | undefined): boolean {
  return a === b || (Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((entry, at) => entry === b[at]))
}
// The number of revisions whose automatic layer order is kept once read.
const AUTOMATIC_ORDER_CACHE = 16
// The trace runs made on the shown artwork, newest first, and the shown run's index.
// A run is a `trace` revision; validate/export records only copy a run's settings, so
// they are passed through without counting. Forward, the chain follows the newest
// child that leads to another run (ignoring edits, which Redo would follow); back, it
// stops at the first revision outside the chain, so stepping never crosses an edit or
// flatten. Off a run the chain is empty (index -1).
export function traceRunChain(revisions: Revision[], revisionId: string | null): { runs: Revision[]; index: number } {
  const byId = new Map(revisions.map(row => [row.id, row]))
  const inChain = (row: Revision | undefined): row is Revision => row?.kind === 'trace' || row?.kind === 'validate' || row?.kind === 'export'
  const parent = (row: Revision) => row.parent_revision_id ? byId.get(row.parent_revision_id) : undefined
  let shown = revisionId ? byId.get(revisionId) : undefined
  const climbed = new Set<string>()
  while (inChain(shown) && shown.kind !== 'trace' && !climbed.has(shown.id)) { climbed.add(shown.id); shown = parent(shown) }
  if (!inChain(shown) || shown.kind !== 'trace') return { runs: [], index: -1 }
  const children = (row: Revision) => revisions.filter(other => other.parent_revision_id === row.id)
  // A validate/export record only continues the chain when a later run hangs off it.
  const leadsToRun = (row: Revision, seen: Set<string>): boolean => {
    if (seen.has(row.id)) return false
    seen.add(row.id)
    return row.kind === 'trace' || (inChain(row) && children(row).some(child => leadsToRun(child, seen)))
  }
  let tip: Revision = shown
  const visited = new Set([tip.id])
  for (;;) {
    const child = children(tip)
      .filter(row => !visited.has(row.id) && leadsToRun(row, new Set(visited)))
      .sort((a, b) => a.created_at.localeCompare(b.created_at)).at(-1)
    if (!child) break
    visited.add(child.id); tip = child
  }
  const runs: Revision[] = []
  const walked = new Set<string>()
  for (let node: Revision | undefined = tip; inChain(node) && !walked.has(node.id); node = parent(node)) {
    walked.add(node.id)
    if (node.kind === 'trace') runs.push(node)
  }
  return { runs, index: runs.findIndex(row => row.id === shown!.id) }
}

// Every trace writes both SVG variants; the overlap file only differs from the
// edge-to-edge one when that run asked for solid underlayers or a positive overlap.
export function tracedSvgKind(trace: Partial<TraceSettings> | undefined): 'traced-overlap' | 'traced-edge' {
  return trace?.solid_underlayers || (trace?.overlap ?? 0) > 0 ? 'traced-overlap' : 'traced-edge'
}

export function tracedSvgName(trace: Partial<TraceSettings> | undefined, physical = false): string {
  return trace?.solid_underlayers ? 'Traced SVG · solid underlayers'
    : (trace?.overlap ?? 0) > 0 ? `Traced SVG · ${trace!.overlap} ${physical ? 'mm' : 'px'} overlap` : 'Traced SVG · edge to edge'
}

// Touch browsers offer no drag handle for a textarea, so it grows to fit its
// text instead; it never shrinks, which keeps a manual resize.
export function growToFit(textarea: HTMLTextAreaElement | null): void {
  if (!textarea) return
  const border = textarea.offsetHeight - textarea.clientHeight
  const needed = textarea.scrollHeight + border
  if (needed > textarea.offsetHeight) textarea.style.height = `${needed}px`
}

function formatElapsed(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

// The single on-canvas overlay for every long-running operation: a title, the
// polite status message, a determinate or indeterminate bar, a visual-only
// elapsed counter, and an optional Cancel action for jobs.
function LoadingOverlay({ title, message, progress, elapsedSeconds, progressLabel, onCancel }: {
  title: string; message: string; progress: number | null; elapsedSeconds: number; progressLabel: string; onCancel?: () => void
}) {
  return <section className="canvas-loading">
    {progress === null && <span className="spinner" aria-hidden="true" />}
    <h2>{title}</h2>
    <div className={progress === null ? 'progress indeterminate' : 'progress'} role="progressbar" aria-label={progressLabel} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress === null ? undefined : Math.round(progress * 100)} style={progress === null ? undefined : { '--progress': progress } as React.CSSProperties}><i /></div>
    {progress !== null && <p className="progress-percent" aria-hidden="true">{Math.round(progress * 100)}%</p>}
    <p role="status" aria-live="polite" aria-atomic="true">{message}</p>
    <p className="elapsed" aria-hidden="true">{formatElapsed(elapsedSeconds)}</p>
    {onCancel && <button className="secondary" onClick={onCancel}>Cancel job</button>}
  </section>
}

export default function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [selected, setSelected] = useState<string | null>(() => hashParameters().get('project'))
  // The selected project as of now, updated in the same call that selects one, so an
  // async continuation can tell whether the project it started in is still selected.
  const selectedRef = useRef(selected)
  function selectProject(id: string | null) { selectedRef.current = id; setSelected(id) }
  // True while `projectId` is still the selected project. Every continuation that sets
  // project-scoped state after an await checks this and drops its result otherwise.
  const stillSelected = (projectId: string | null | undefined) => Boolean(projectId) && selectedRef.current === projectId
  const [job, setJob] = useState<Job | null>(null)
  const [revision, setRevision] = useState<string | null>(() => hashParameters().get('revision'))
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  // The revision `artifacts` were resolved for. `revision` changes first and the
  // artifacts follow, so everything describing the shown artwork derives from this.
  const [artifactsRevision, setArtifactsRevision] = useState<string | null>(null)
  const [revisions, setRevisions] = useState<Revision[]>([])
  const [error, setError] = useState('')
  // The error message each raster lookup last reported, so its own later success clears
  // exactly that message and never an unrelated one.
  const lookupErrors = useRef<{ cleanup?: string; edit?: string }>({})
  // Editor-state requests shared by every effect that needs the same revision at once;
  // each is cancelled when its last user aborts.
  const editorRequests = useRef(new Map<string, { state: Promise<EditorState>; controller: AbortController; users: number }>())
  function requestEditor(revisionId: string, signal: AbortSignal): Promise<EditorState> {
    // An already-aborted caller never fires 'abort', so it must not count as a user.
    if (signal.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'))
    const requests = editorRequests.current
    let entry = requests.get(revisionId)
    if (!entry) {
      const controller = new AbortController()
      const created = { state: api.editor(revisionId, controller.signal), controller, users: 0 }
      created.state.catch(() => { if (requests.get(revisionId) === created) requests.delete(revisionId) })
      requests.set(revisionId, created)
      entry = created
    }
    const shared = entry
    shared.users += 1
    signal.addEventListener('abort', () => {
      shared.users -= 1
      // React runs a commit's effect cleanups before its setups, so wait a microtask:
      // an effect re-subscribing in the same commit reuses the request (or its result).
      queueMicrotask(() => {
        if (shared.users > 0) return
        shared.controller.abort()
        if (requests.get(revisionId) === shared) requests.delete(revisionId)
      })
    }, { once: true })
    return shared.state
  }
  function reportLookup(lookup: 'cleanup' | 'edit', message: string | null) {
    const previous = lookupErrors.current[lookup]
    lookupErrors.current[lookup] = message ?? undefined
    if (message) setError(message)
    else if (previous) setError(shown => shown === previous ? '' : shown)
  }
  const [maxUploadBytes, setMaxUploadBytes] = useState(100 * 1024 * 1024)
  const [handoff, setHandoff] = useState<InkscapeHandoff | null>(null)
  const [handoffMessage, setHandoffMessage] = useState('')
  const [frame, setFrame] = useState('landscape')
  const [customRatio, setCustomRatio] = useState<Dimensions>([16, 9])
  const [stage, setStage] = useState<WorkflowStage>(() => stageFromHash(window.location.hash))
  const [view, setView] = useState<ArtworkView>('source')
  // The view a just-finished flatten or trace should open on, held until the revision
  // it produced has loaded far enough to offer that view.
  const [pendingView, setPendingView] = useState<{ view: ArtworkView; revision: string } | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<{ id: string; name: string } | null>(null)
  const [deleting, setDeleting] = useState(false)
  // The flattened original behind the shown artwork's edits (the Flattened view), with
  // the palette revision that wrote it. Resolved inside the artifacts load and committed
  // with them; reused while that palette revision is unchanged; a failed lookup is not
  // kept, so the next revision change retries it.
  const [preEdit, setPreEdit] = useState<{ paletteId: string; flattened: Artifact } | null>(null)
  const preEditRef = useRef(preEdit)
  preEditRef.current = preEdit
  // The raster the shown artwork's cleanup started from (the Flattened/Edited views),
  // with that cleanup's revision id. It is resolved inside the artifacts load and
  // committed with them, so it always matches the shown artifacts; it depends only on
  // the cleanup, so revisions below the same cleanup reuse it without a lookup. A
  // failed lookup is not kept, so the next revision change retries it.
  const [preCleanup, setPreCleanup] = useState<{ cleanupId: string; flattened: Artifact } | null>(null)
  const preCleanupRef = useRef(preCleanup)
  preCleanupRef.current = preCleanup
  const [projectLoading, setProjectLoading] = useState(false)
  // A same-project revision change keeps the shown artwork while the new artifacts load.
  const [revisionLoading, setRevisionLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  // null while bytes are still transferring is never used; during transfer this is
  // the 0-1 fraction, and it flips to null once upload completes and the server is
  // processing (indeterminate).
  const [uploadFraction, setUploadFraction] = useState<number | null>(0)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [paletteSettings, setPaletteSettings] = useState<PaletteSettings>(DEFAULT_PALETTE_SETTINGS)
  const [traceSettings, setTraceSettings] = useState<TraceSettings>(DEFAULT_TRACE_SETTINGS)
  const [cleanupSettings, setCleanupSettings] = useState<CleanupSettings>(DEFAULT_CLEANUP_SETTINGS)
  // Position within the flatten settings history: 0 is the working form (the newest
  // recorded iteration, plus any un-run edits), higher indices are older iterations.
  // The working form is stashed on first step back so stepping forward restores the
  // un-run edits it replaced.
  const [paletteStep, setPaletteStep] = useState(0)
  const paletteLive = useRef<PaletteSettings | null>(null)
  // The Cleanup form's settings history, stepped exactly like Flatten's.
  const [cleanupStep, setCleanupStep] = useState(0)
  const cleanupLive = useRef<CleanupSettings | null>(null)
  const traceDraftSpent = useRef<string | null>(null)
  // Pixel-era trace settings (a recorded run the server could not convert, a draft saved
  // before units) waiting for the traced grid of `revision` to convert on.
  const pixelTrace = useRef<{ project: string; revision: string; stored?: Partial<TraceSettings>; draft?: Partial<TraceSettings> } | null>(null)
  // A Create form mounted while another stage was showing measured nothing, so
  // fit the subject again whenever Create becomes visible.
  const subjectField = useRef<HTMLTextAreaElement | null>(null)
  useEffect(() => { if (stage === 'create') growToFit(subjectField.current) }, [stage])
  // A re-run of a stale cleanup with its own settings leaves the Cleanup form's draft.
  const cleanupDraftSpent = useRef<string | null>(null)
  const [selectionMode, setSelectionMode] = useState<SelectionMode>(null)
  const [distinctFirst, setDistinctFirst] = useState<[number, number] | null>(null)
  // The canvas marker of the preservation row hovered or focused in the Flatten panel.
  // Marker ids are list indices, so a highlight holds only for the point lists it was
  // made against: any change to them (a pick, a removal, a reset, stepping iterations)
  // drops it, and an id never highlights the point that moved into its index.
  const [highlight, setHighlight] = useState<{ id: string; lists: string } | null>(null)
  const [regionSelection, setRegionSelection] = useState<Component[]>([])
  const [flattenedPixels, setFlattenedPixels] = useState<Pixels | null>(null)
  // The revision `flattenedPixels` belong to; area/pencil edits act only on that one.
  const [pixelsRevision, setPixelsRevision] = useState<string | null>(null)
  const [editorPalette, setEditorPalette] = useState<PaletteColour[]>([])
  // The palette index of the colour the active cleanup removes, read with `editorPalette`
  // (EditorState.removed_colour_index): by index, since a trace's colour names may have
  // given the name the cleanup recorded to another colour of this palette.
  const [removedIndex, setRemovedIndex] = useState<number | null>(null)
  // The removed colour the Cleanup form inherits, resolved by the server against the
  // palette a Cleanup of `revision` starts from (EditorState.inherited_remove_colour,
  // pipeline.inherited_remove_colour): a hex or current name that identifies the last
  // cleanup's colour there, or null and `unresolved` when none does. Null state: no
  // cleanup removed a colour, or it is not read yet.
  const [inheritedRemove, setInheritedRemove] = useState<InheritedRemove | null>(null)
  // The revision `editorPalette` was read from. A load keeps the last palette listed, but
  // only the selected revision's own may be used to write a layer order.
  const [paletteRevision, setPaletteRevision] = useState<string | null>(null)
  // Each revision's automatic bottom-to-top order (the tracer's own inference), which
  // colours an override leaves unnamed follow, keyed by revision (a revision's order never
  // changes; the AUTOMATIC_ORDER_CACHE most recent are kept). Read only while layer order
  // can matter: otherwise the tracer infers none and no order is listed.
  const [automaticOrders, setAutomaticOrders] = useState<ReadonlyMap<string, string[] | null>>(new Map())
  // The last failed read of a revision's automatic order, shown as the list's lock reason.
  const [automaticOrderError, setAutomaticOrderError] = useState<{ revision: string; message: string } | null>(null)
  const [automaticOrderAttempt, setAutomaticOrderAttempt] = useState(0)
  // The layer order a trace run leaves out because order cannot matter. Before the run
  // starts it is written into the persisted trace draft unless that draft already names one
  // (so a reload or a run from an older trace keeps it); this remembers the draft's order
  // then, for the run that spends that draft on success.
  const traceOrderHeld = useRef<{ jobId: string; order: string[] } | null>(null)
  // A trace job whose start is reading the revision's palette first; the job buttons wait.
  const [startingJob, setStartingJob] = useState(false)
  const startRead = useRef<AbortController | null>(null)
  const starting = useRef(false)
  // Pixel size of the image the trace stage fits curves to (the flattened/edited grid).
  // The traced grid and the revision it was measured for. A load keeps the last one on
  // display (the live preview pauses until the new revision's grid is known).
  const [tracedGrid, setTracedGrid] = useState<{ revision: string; width: number; height: number } | null>(null)
  const tracedSize = useMemo(() => tracedGrid ? { width: tracedGrid.width, height: tracedGrid.height } : null, [tracedGrid])
  // Why the traced grid of a revision could not be measured, so the paused preview says so.
  const [tracedGridError, setTracedGridError] = useState<{ revision: string; message: string } | null>(null)
  // The revision whose artifacts failed to load: the previous artwork stays shown, not
  // loading, until the next revision change retries.
  const [failedRevision, setFailedRevision] = useState<string | null>(null)
  // The error the last project/artifacts load reported (Retry offers itself only while that
  // error is the one shown, and clears only it), and a counter a Retry bumps to reload.
  const [loadError, setLoadError] = useState<string | null>(null)
  const [reloadCount, setReloadCount] = useState(0)
  function retryLoad() { setError(shown => shown === loadError ? '' : shown); setReloadCount(count => count + 1) }
  const [overlayFrame, setOverlayFrame] = useState<OverlayFrame | undefined>(undefined)
  const [paletteDraft, setPaletteDraft] = useState<{ index: number; hex: string } | null>(null)
  const [palettePreviewFrame, setPalettePreviewFrame] = useState<OverlayFrame | undefined>(undefined)
  // The projects with an area edit in flight; the busy state shows only on its own project.
  const [regionBusyProjects, setRegionBusyProjects] = useState<string[]>([])
  const regionBusy = Boolean(selected) && regionBusyProjects.includes(selected!)
  const [regionError, setRegionError] = useState('')
  const [editTool, setEditTool] = useState<'areas' | 'pencil'>('areas')
  const [pencilLabel, setPencilLabel] = useState<number | null>(null)
  const [paintedCells, setPaintedCells] = useState<Map<number, number>>(new Map())
  // The projects with a pencil edit in flight, shown busy only on their own project.
  const [pencilBusyProjects, setPencilBusyProjects] = useState<string[]>([])
  const pencilBusy = Boolean(selected) && pencilBusyProjects.includes(selected!)
  const [pencilError, setPencilError] = useState('')
  const [desktopWorkflow, setDesktopWorkflow] = useState(() => window.innerWidth > 700)
  const [upscaleOpen, setUpscaleOpen] = useState(false)
  const stageScroll = useRef<Partial<Record<WorkflowStage, number>>>({})
  const loadedProject = useRef<string | null>(null)
  // Mirrors the live selection so a region edit resolving asynchronously can read
  // areas clicked after it was kicked off.
  const regionSelectionRef = useRef<Component[]>(regionSelection)
  // A region edit bumps the revision, which reloads artifacts and would otherwise
  // clear the selection. This hands the surviving selection to that reload so
  // post-kickoff clicks are kept; `applied` guards the two-phase revision→artifacts
  // reload from clobbering areas clicked between its two runs.
  const preserveRegion = useRef<{ id: string; selection: Component[]; applied: boolean } | null>(null)
  // The exact pixels an area/pencil edit produces, computed client-side from the same
  // components the server reassigns. When the resulting revision loads, the decode
  // effect adopts this buffer instead of re-fetching and re-decoding the full flattened
  // PNG, since the two are identical (both are four-connected exact-palette regions).
  const patchedFlattened = useRef<{ revision: string; pixels: Pixels } | null>(null)
  // The revision the pixel effect last ran for: its re-runs for the same revision (the
  // artifacts landing after an adopted edit) keep the staged pencil strokes.
  const pixelEffectRevision = useRef<string | null>(null)
  const current = revisions.find(row => row.id === revision)
  const flattened = artifacts.find(row => row.kind === 'flattened')
  // The settings that produced the shown image, recorded on its nearest ancestor.
  const pipelineBaseline = useMemo(() => inheritedPipelineSettings(revisions, current?.id || null), [revisions, current?.id])
  // Pick the variant from the settings of the run that produced the loaded SVGs, so the
  // choice and its label always describe the file shown, even mid-reload.
  const tracedRunId = artifacts.find(row => TRACED_SVG_KINDS.has(row.kind))?.revision_id
  const tracedRunSettings = (revisions.find(row => row.id === tracedRunId)?.settings.trace ?? pipelineBaseline.trace) as Partial<TraceSettings> | undefined
  const tracedSvg = artifacts.find(row => row.kind === tracedSvgKind(tracedRunSettings))
  const cleanupLookup = useMemo(() => nearestCleanup(revisions, current?.id || null), [revisions, current?.id])
  const activeCleanup = cleanupLookup.stale ? null : cleanupLookup.revision
  // The artwork changed after the last Cleanup, so tracing waits for Cleanup to re-run.
  const cleanupStale = cleanupLookup.stale
  // The Cleaned view exists only when the active cleanup changed pixels, i.e. it wrote
  // its own raster; a cleanup run with cleanup off keeps the uncleaned one.
  // Judged against the revision the artifacts belong to, never the newer `revision`
  // whose artifacts are still loading.
  const shownCleanupLookup = useMemo(() => nearestCleanup(revisions, artifactsRevision), [revisions, artifactsRevision])
  const shownCleanup = shownCleanupLookup.stale ? null : shownCleanupLookup.revision
  const hasCleanup = Boolean(shownCleanup) && flattened?.revision_id === shownCleanup?.id
  const preCleanupFlattened = hasCleanup && preCleanup && preCleanup.cleanupId === shownCleanup?.id ? preCleanup.flattened : null
  // The before-cleanup lookup failed: the Flattened/Edited views have nothing true to show.
  const beforeCleanupMissing = hasCleanup && !preCleanupFlattened
  // The raster Palette and Edit decode and edit: the one from before the cleanup. None
  // while the shown artifacts belong to another revision, so no edit acts on them.
  const artifactsCurrent = Boolean(revision) && artifactsRevision === revision
  // While the shown artifacts are another revision's, nothing may act on the selected
  // one on their evidence: edits and jobs wait, saying why.
  const selectedUnavailable = !revision || artifactsCurrent ? null
    : failedRevision === revision ? 'The selected revision failed to load.' : 'Loading the selected revision…'
  // Whether the selected revision itself has a segmentation to clean up or trace.
  const selectedHasSegmentation = artifactsCurrent && artifacts.some(row => row.kind === 'segmentation')
  const selectedFailed = Boolean(revision) && failedRevision === revision
  // The stage headings hold steady through a load (they describe the shown artwork) and
  // only change once the selected revision's own state is known or has failed.
  const readiness = selectedFailed ? 'Unavailable' : artifacts.some(row => row.kind === 'segmentation') ? 'Ready' : 'Flatten first'
  // Why a stage's controls are locked: announced to assistive tech through the buttons'
  // aria-describedby, and shown (below the controls, so nothing shifts during a normal
  // load) only once the revision has failed, with a way to retry.
  const lockReason = (id: string) => selectedUnavailable
    ? <p id={id} className={selectedFailed ? 'stage-guidance lock-reason' : 'visually-hidden'}>{selectedUnavailable}{selectedFailed && <> <button type="button" className="secondary" onClick={retryLoad}>Retry</button></>}</p>
    : null
  const lockedBy = (id: string) => selectedUnavailable ? id : undefined
  const editableFlattened = !artifactsCurrent ? null : hasCleanup ? preCleanupFlattened : flattened ?? null
  const { hasFlatten, hasEdit, paletteAncestorId } = useMemo(() => editAncestry(revisions, artifactsRevision), [artifactsRevision, revisions])
  const preEditFlattened = hasEdit && preEdit && preEdit.paletteId === paletteAncestorId ? preEdit.flattened : null
  // The pre-edit original is unavailable (its lookup failed): the Flattened view has nothing true to show.
  const preEditMissing = hasEdit && !preEditFlattened
  const source = artifacts.find(row => row.kind === 'source')
  const flattenAvailable = hasFlatten || Boolean(flattened)
  // Palette and Edit always act on the raster from before any cleanup (the server's
  // editor reads it too), so the Cleaned image is never the editable one.
  const editableView: ArtworkView = hasEdit ? 'edited' : 'flattened'
  const defaultView: ArtworkView = hasCleanup ? 'cleaned' : hasEdit ? 'edited' : flattenAvailable ? 'flattened' : 'source'
  const viewOptions: { id: ArtworkView; label: string; available: boolean }[] = [
    { id: 'source', label: 'Source', available: Boolean(source) },
    { id: 'flattened', label: 'Flattened', available: flattenAvailable && (hasEdit ? !preEditMissing : !beforeCleanupMissing) },
    { id: 'edited', label: 'Edited', available: hasEdit && !beforeCleanupMissing },
    { id: 'cleaned', label: 'Cleaned', available: hasCleanup },
    { id: 'traced', label: 'Traced', available: Boolean(tracedSvg) },
  ]
  const availableViews = viewOptions.filter(option => option.available).map(option => option.id)
  const editSelectable = view === editableView
  const editableLabel = hasEdit ? 'Edited' : 'Flattened'
  const artworkStageActive = ['flatten', 'cleanup', 'trace', 'palette', 'edit'].includes(stage) || Boolean(selectionMode)
  const viewToggle = () => availableViews.length > 1
    ? <div className="view-toggle" role="group" aria-label="Artwork view">{viewOptions.filter(option => option.available).map(option => <button key={option.id} type="button" className={view === option.id ? 'active' : ''} aria-pressed={view === option.id} onClick={() => setView(option.id)}>{option.label}</button>)}</div>
    : null
  // The baseline is the settings that produced the shown image (recorded on the
  // nearest ancestor revision), falling back to defaults before the step has run.
  // Fields diverging from it are flagged as pending edits.
  const baselinePalette = useMemo<PaletteSettings>(() => ({ ...DEFAULT_PALETTE_SETTINGS, ...pipelineBaseline.palette }), [pipelineBaseline])
  const baselineTrace = useMemo<TraceSettings>(() => ({ ...DEFAULT_TRACE_SETTINGS, ...pipelineBaseline.trace }), [pipelineBaseline])
  // Its removed colour is the server's resolution once read for this revision.
  const baselineCleanup = useMemo<CleanupSettings>(() => {
    const base = { ...DEFAULT_CLEANUP_SETTINGS, ...pipelineBaseline.cleanup }
    return inheritedRemove?.revision === revision && base.remove_colour != null ? { ...base, remove_colour: inheritedRemove.value ?? base.remove_colour } : base
  }, [pipelineBaseline, inheritedRemove, revision])
  // The last cleanup's removed colour is not exactly one colour of this palette and the
  // form still holds it unedited: Cleanup runs and previews nothing until the user picks
  // a colour or clears the field, so it never silently removes nothing (or another colour).
  const removeBlocked = inheritedRemove?.revision === revision && inheritedRemove.unresolved
    && cleanupSettings.remove_colour != null && cleanupSettings.remove_colour === baselineCleanup.remove_colour
    ? unresolvedRemoveReason(cleanupSettings.remove_colour, inheritedRemove.problem)
    : null
  // Until this revision's editor state says what the inherited colour resolves to, a Run
  // could send the inherited hex unchecked, so it waits.
  const removeChecking = baselineCleanup.remove_colour != null && paletteRevision !== revision ? 'Checking the colour to remove…' : null
  const cleanupRunBlocked = removeBlocked ?? removeChecking
  const paletteIterations = useMemo(() => ancestorPaletteIterations(revisions, current?.id || null), [revisions, current?.id])
  const cleanupIterations = useMemo(() => ancestorCleanupIterations(revisions, current?.id || null), [revisions, current?.id])
  const traceRuns = useMemo(() => traceRunChain(revisions, current?.id || null), [revisions, current?.id])
  function stepPalette(delta: number) {
    const next = Math.max(0, Math.min(paletteIterations.length - 1, paletteStep + delta))
    if (next === paletteStep) return
    if (paletteStep === 0) paletteLive.current = paletteSettings
    setPaletteSettings(next === 0 ? (paletteLive.current ?? paletteSettings) : paletteIterations[next])
    setPaletteStep(next)
  }
  function stepCleanup(delta: number) {
    const next = Math.max(0, Math.min(cleanupIterations.length - 1, cleanupStep + delta))
    if (next === cleanupStep) return
    if (cleanupStep === 0) cleanupLive.current = cleanupSettings
    setCleanupSettings(next === 0 ? (cleanupLive.current ?? cleanupSettings) : cleanupIterations[next])
    setCleanupStep(next)
  }
  // Trace iterations are the saved runs themselves: a step makes that run the active
  // revision and lands on its traced SVG.
  function stepTrace(delta: number) {
    const target = traceRuns.runs[traceRuns.index + delta]
    if (!target || traceRuns.index < 0) return
    setPendingView({ view: 'traced', revision: target.id })
    setRevision(target.id)
  }
  const sourceRevision = revisions.find(row => row.id === source?.revision_id)
  // The source an upscaled source was made from (its parent's effective source), loaded
  // only for the canvases that compare with it: Upload, and the Palette and Edit Before
  // picker. A failed load is reported in the canvas's Compare control, whose Retry
  // re-runs it.
  const upscaledFrom = sourceRevision?.kind === 'upscaled-source' ? sourceRevision.parent_revision_id : null
  const preUpscaleWanted = Boolean(upscaledFrom) && (stage === 'upload' || stage === 'palette' || stage === 'edit')
  const [originalSource, setOriginalSource] = useState<{ revisionId: string; source: Artifact } | null>(null)
  const [originalSourceFailure, setOriginalSourceFailure] = useState<{ revisionId: string; message: string } | null>(null)
  const [originalSourceAttempt, setOriginalSourceAttempt] = useState(0)
  // The upscale's recorded grids, through which the pre-upscale source registers with
  // the upscaled one; on revisions recorded before them, the canvas uses the two images'
  // own sizes.
  const upscaleBefore = recordedGrid(sourceRevision?.settings.parent_source_size)
  const upscaleAfter = recordedGrid(sourceRevision?.settings.source_size)
  // Whether the canvas compares (Split or Blink): Palette and Edit then commit nothing.
  const [canvasComparing, setCanvasComparing] = useState(false)
  useEffect(() => {
    if (!upscaledFrom || !preUpscaleWanted || originalSource?.revisionId === upscaledFrom) return
    const controller = new AbortController()
    setOriginalSourceFailure(null)
    api.effectiveArtifacts(upscaledFrom, controller.signal).then(rows => {
      const found = rows.find(row => row.kind === 'source')
      if (!found) throw new Error('the upscaled revision has no source before it')
      setOriginalSource({ revisionId: upscaledFrom, source: found })
    }).catch(cause => {
      if (!controller.signal.aborted) setOriginalSourceFailure({ revisionId: upscaledFrom, message: `Could not load the source from before the upscale: ${String(cause)}` })
    })
    return () => controller.abort()
  }, [upscaledFrom, preUpscaleWanted, originalSourceAttempt])
  const preUpscaleSource = upscaledFrom && originalSource?.revisionId === upscaledFrom ? originalSource.source : null
  const preUpscaleFailure = preUpscaleWanted && originalSourceFailure?.revisionId === upscaledFrom && !preUpscaleSource ? originalSourceFailure.message : null
  const generation = generationFromRevision(sourceRevision)

  useEffect(() => { api.projects().then(setProjects).catch(e => setError(String(e))) }, [])
  useEffect(() => { api.health().then(health => setMaxUploadBytes(health.max_upload_bytes)).catch(() => { /* keep the default cap */ }) }, [])
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
      if (next.kind === 'palette' && next.project_id) clearDraft(next.project_id, 'palette')
      if (next.kind === 'cleanup' && next.project_id && cleanupDraftSpent.current === next.id) clearDraft(next.project_id, 'cleanup')
      if (next.kind === 'trace' && next.project_id && traceDraftSpent.current === next.id) {
        const held = traceOrderHeld.current?.jobId === next.id ? traceOrderHeld.current.order : null
        const pending = held ? draftTraceOrder(next.project_id) : undefined
        clearDraft(next.project_id, 'trace')
        // The withheld order survives the spend; an order the user saved during the run wins.
        if (held) keepTraceDraftOrder(next.project_id, sameOrder(pending, held) ? held : pending ?? null)
      }
      // The result lands only in its own project; one that finishes after the user
      // switched away leaves the now-selected project's revision, view and stage alone.
      if (!stillSelected(next.project_id)) return
      setRevision(next.result_revision_id)
      if (next.kind === 'palette') setPendingView({ view: 'flattened', revision: next.result_revision_id })
      if (next.kind === 'cleanup') setPendingView({ view: 'cleaned', revision: next.result_revision_id })
      if (next.kind === 'trace') setPendingView({ view: 'traced', revision: next.result_revision_id })
      // A finished source generation lands the user on Flatten, the next step, so
      // the freshly generated artwork is ready to reduce without hunting for the tab.
      if (next.kind === 'generate') navigateStage('flatten')
    }
  }, () => setError('Lost the live job connection')) : undefined, [job?.id])
  useEffect(() => {
    if (!selected) { setArtifacts([]); setArtifactsRevision(null); setPreCleanup(null); setRevision(null); setRevisions([]); setRevisionLoading(false); loadedProject.current = null; return }
    const controller = new AbortController()
    // Only blank the canvas when switching projects; an edit that swaps revisions
    // within the same project keeps its artwork (and its zoom/pan) until the new
    // artifacts arrive.
    const switchingProject = loadedProject.current !== selected
    // Drop any adopted-buffer left from the previous project so its ~hundreds of MB do
    // not linger; a revision id never collides across projects, so this is purely tidying.
    // Staged pencil strokes belong to the project being left.
    if (switchingProject) { setPaintedCells(new Map()); pixelEffectRevision.current = null }
    if (switchingProject) { setProjectLoading(true); setArtifacts([]); setArtifactsRevision(null); setPreCleanup(null); preCleanupRef.current = null; setPreEdit(null); preEditRef.current = null; patchedFlattened.current = null }
    setRevisionLoading(!switchingProject)
    setFailedRevision(null); setLoadError(null)
    api.project(selected, controller.signal).then(async project => {
      if (controller.signal.aborted) return
      setRevisions(project.revisions)
      const latest = revision && project.revisions.some(row => row.id === revision)
        ? revision : project.revisions[0]?.id
      setRevision(latest || null)
      // The pre-edit original depends only on the ancestry, so it loads alongside; a
      // failed artifacts request cancels it, so only that failure is reported.
      const sibling = new AbortController()
      controller.signal.addEventListener('abort', () => sibling.abort(), { once: true })
      const [rows, original] = await Promise.all([
        latest ? api.effectiveArtifacts(latest, controller.signal).catch(cause => { sibling.abort(); throw cause }) : Promise.resolve([]),
        latest ? preEditRaster(project.revisions, latest, sibling.signal) : Promise.resolve({ raster: null, error: null }),
      ])
      const before = latest ? await beforeCleanupRaster(project.revisions, latest, rows, controller.signal) : { raster: null, error: null }
      // A newer load superseded this one while its last request was settling.
      if (controller.signal.aborted) return
      // One commit: the artifacts, whose revision they are, the rasters from before
      // their cleanup and their edits, and those lookups' outcomes.
      reportLookup('cleanup', before.error); reportLookup('edit', original.error)
      setArtifacts(rows); setArtifactsRevision(latest || null); setPreCleanup(before.raster); setPreEdit(original.raster)
      loadedProject.current = selected
    }).catch(cause => {
      if (!controller.signal.aborted) { setError(String(cause)); setFailedRevision(revision); setLoadError(String(cause)) }
    }).finally(() => { if (!controller.signal.aborted) { setProjectLoading(false); setRevisionLoading(false) } })
    return () => controller.abort()
  }, [selected, revision, reloadCount])
  useEffect(() => {
    const syncStage = () => {
      stageScroll.current[stage] = window.scrollY
      const parameters = hashParameters()
      const next = stageFromHash(window.location.hash)
      setStage(next)
      if (next !== 'flatten' && next !== 'trace') { setSelectionMode(null); setDistinctFirst(null) }
      selectProject(parameters.get('project'))
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
    // Only the selected revision's own artifacts say whether it has SVGs to hand off.
    if (revision && artifactsCurrent && artifacts.some(row => row.media_type === 'image/svg+xml')) {
      const controller = new AbortController()
      api.inkscapeHandoff(revision, controller.signal).then(setHandoff).catch(cause => { if (!controller.signal.aborted) setError(String(cause)) })
      return () => controller.abort()
    }
  }, [revision, artifactsCurrent, artifacts.map(row => row.id).join(',')])
  useEffect(() => {
    // Fill each stage's fields from the settings that produced the shown revision
    // (defaults before it ran), then overlay any pending per-step draft so un-run
    // edits survive a refresh until the stage is run. An older trace run shows exactly
    // its own settings; the trace draft belongs to the newest run.
    const { palette: storedPalette, cleanup: storedCleanup, trace: storedTrace, recordedRemoveColour } = inheritedPipelineSettings(revisions, current?.id || null)
    let draft: { palette?: Partial<PaletteSettings>; palette_revision?: string; cleanup?: Partial<CleanupSettings>; trace?: Partial<TraceSettings> } = {}
    try { draft = selected ? JSON.parse(localStorage.getItem(draftKey(selected)) || '{}') : {} } catch { /* use revision values */ }
    // A trace draft saved before the cleanup stage carries the size, removed colour and
    // cleanup; move them into the Cleanup draft, where they now belong, and store that.
    const legacyDraft = draft.trace && legacyCleanupSettings(draft.trace as Record<string, unknown>)
    if (selected && legacyDraft) {
      draft = { ...draft, cleanup: { ...legacyDraft, ...draft.cleanup }, trace: normalizeTraceSettings(draft.trace!) }
      localStorage.setItem(draftKey(selected), JSON.stringify(draft))
    }
    // A cleanup draft holding the inherited cleanup's removed colour as recorded (a draft
    // saved while the form showed the recorded name) is not a choice of the user's: that
    // name may since belong to another colour, so it is dropped and the inherited value,
    // which names the cleanup's colour, wins. A different value is the user's and stays.
    if (selected && draft.cleanup?.remove_colour != null && draft.cleanup.remove_colour === recordedRemoveColour) {
      const { remove_colour: _dropped, ...rest } = draft.cleanup
      draft = { ...draft, cleanup: rest }
      localStorage.setItem(draftKey(selected), JSON.stringify(draft))
    }
    // A palette draft saved before drafts named their revision was picked on an
    // un-upscaled source (no upscale then recorded its grids): name the shown revision.
    if (selected && current && draft.palette && !draft.palette_revision) {
      draft = { ...draft, palette_revision: current.id }
      localStorage.setItem(draftKey(selected), JSON.stringify(draft))
    }
    // The palette draft's points were picked on the source of the revision it names.
    const draftPalette = draft.palette && draft.palette_revision && current
      ? palettePointsForRevision(draft.palette, revisions, draft.palette_revision, current.id) : draft.palette
    setPaletteSettings({ ...DEFAULT_PALETTE_SETTINGS, ...storedPalette, ...draftPalette })
    setCleanupSettings({ ...DEFAULT_CLEANUP_SETTINGS, ...storedCleanup, ...draft.cleanup })
    setCleanupStep(0); cleanupLive.current = null
    // Trace lengths are thousandths of the traced image's longer side. A draft saved
    // before units (no length_unit) holds pixels, as does a recorded run the server could
    // not convert ('px'); those wait in `pixelTrace` until this revision's traced grid is
    // measured, then convert on it (the effect below).
    const draftTrace = traceRuns.index > 0 || !draft.trace ? undefined : normalizeTraceSettings(draft.trace)
    const pixelStored = storedTrace && isPixelRecord(storedTrace) ? storedTrace : undefined
    const pixelDraft = draftTrace && isPixelRecord(draftTrace) ? draftTrace : undefined
    pixelTrace.current = selected && current && (pixelStored || pixelDraft)
      ? { project: selected, revision: current.id, stored: pixelStored, draft: pixelDraft } : null
    setTraceSettings({ ...DEFAULT_TRACE_SETTINGS, ...(pixelStored ? {} : storedTrace), ...(pixelDraft ? {} : draftTrace) })
    setPaletteStep(0); paletteLive.current = null
    setSelectionMode(null); setDistinctFirst(null)
  }, [current?.id, selected])
  // Pixel-era trace settings held back by the fill above convert on the traced grid of the
  // revision they were loaded for, once it is measured; a converted draft is stored back.
  useEffect(() => {
    const pending = pixelTrace.current
    if (!pending || !tracedGrid || tracedGrid.revision !== pending.revision || pending.project !== selected) return
    pixelTrace.current = null
    const longSide = Math.max(tracedGrid.width, tracedGrid.height)
    const stored = pending.stored && pixelRecordInUnits(pending.stored, longSide)
    const draft = pending.draft && pixelRecordInUnits(pending.draft, longSide)
    setTraceSettings(settings => ({ ...settings, ...stored, ...draft }))
    if (draft) {
      let saved: { trace?: Partial<TraceSettings> } = {}
      try { saved = JSON.parse(localStorage.getItem(draftKey(pending.project)) || '{}') } catch { saved = {} }
      localStorage.setItem(draftKey(pending.project), JSON.stringify({ ...saved, trace: draft }))
    }
  }, [tracedGrid, selected, current?.id])
  // Once the server has resolved the inherited removed colour for this revision, the
  // form takes it in place of the value it inherited before reading it (the cleanup's
  // hex, which a recolour can give two colours), unless the user changed that value.
  // Declared after the fill above, so a re-fill is refined in the same commit.
  useEffect(() => {
    if (!inheritedRemove || inheritedRemove.revision !== revision || cleanupStep !== 0) return
    const inherited = pipelineBaseline.cleanup?.remove_colour
    if (inherited == null) return
    setCleanupSettings(settings => settings.remove_colour === inherited ? { ...settings, remove_colour: inheritedRemove.value ?? inherited } : settings)
  }, [inheritedRemove, current?.id])
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
  useEffect(() => { regionSelectionRef.current = regionSelection }, [regionSelection])
  useEffect(() => {
    // A region edit hands its surviving selection to `preserveRegion` for exactly the
    // revision it produced; restore it once, keep it across the reload's second run, and
    // clear the selection for any other revision change.
    const decision = regionReloadDecision(preserveRegion.current, revision)
    if (decision.action === 'apply') { setRegionSelection(decision.selection); preserveRegion.current = decision.handoff }
    else if (decision.action === 'clear') { preserveRegion.current = null; setRegionSelection([]) }
    setRegionError('')
    if (pixelEffectRevision.current !== revision) { setPaintedCells(new Map()); setPencilError('') }
    pixelEffectRevision.current = revision
    // An area/pencil edit already knows exactly which pixels its revision holds, so
    // adopt that pre-computed buffer (at once, before the revision's artifacts arrive)
    // rather than decoding a fresh 89-megapixel PNG. The reference is dropped on
    // navigating away, and once the adopting run sees the artifacts current (the pixel
    // state then owns the buffer), so it never pins a second one.
    if (patchedFlattened.current && patchedFlattened.current.revision !== revision) patchedFlattened.current = null
    const patched = revision && patchedFlattened.current?.revision === revision ? patchedFlattened.current : null
    // The palette is left to the revision's editor state (below): a same-project load
    // keeps the last one shown until the new revision's arrives.
    if (!revision || (!editableFlattened && !patched)) { setFlattenedPixels(null); setPixelsRevision(null); return }
    const controller = new AbortController()
    requestEditor(revision, controller.signal).then(state => { if (!controller.signal.aborted) { setEditorPalette(state.palette); setRemovedIndex(state.removed_colour_index ?? null); setInheritedRemove(inheritedRemoveOf(revision, state)); setPaletteRevision(revision) } })
      .catch(cause => { if (!controller.signal.aborted) setRegionError(String(cause)) })
    if (patched) {
      setFlattenedPixels(patched.pixels); setPixelsRevision(revision)
      if (editableFlattened) patchedFlattened.current = null
      return () => controller.abort()
    }
    if (!editableFlattened) return () => controller.abort()
    const image = new Image()
    image.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = image.naturalWidth; canvas.height = image.naturalHeight
      const context = canvas.getContext('2d')
      if (!context) return
      context.drawImage(image, 0, 0)
      try {
        const data = context.getImageData(0, 0, canvas.width, canvas.height)
        setFlattenedPixels({ data: data.data, width: canvas.width, height: canvas.height }); setPixelsRevision(revision)
      } catch { /* canvas unavailable; region selection stays disabled */ }
    }
    image.src = editableFlattened.download_url
    return () => { controller.abort(); image.onload = null }
  }, [revision, editableFlattened?.id])
  useEffect(() => {
    // The traced grid and palette are the current revision's (cleaned raster's), whatever the editable
    // raster; measured once that revision's artifacts are current.
    // No flatten behind the shown revision (or a project switch cleared its artifacts):
    // no grid and no palette. A same-project load keeps both until the new ones arrive.
    if (!revision || !flattened) { setTracedGrid(null); setEditorPalette([]); setRemovedIndex(null); setInheritedRemove(null); setPaletteRevision(null); return }
    if (!artifactsCurrent) return
    // A new measurement supersedes the last failure: the preview says only "paused" meanwhile.
    setTracedGridError(null)
    const controller = new AbortController()
    requestEditor(revision, controller.signal).then(state => {
      if (controller.signal.aborted) return
      setTracedGrid({ revision, width: state.width, height: state.height }); setEditorPalette(state.palette); setRemovedIndex(state.removed_colour_index ?? null); setInheritedRemove(inheritedRemoveOf(revision, state)); setPaletteRevision(revision)
    }).catch(cause => {
      if (controller.signal.aborted) return
      // Unmeasured: the preview pauses on this revision, saying why. The palette is left as
      // it is, since only a successful read of this revision ever makes it this revision's.
      setTracedGridError({ revision, message: String(cause) })
      setTracedGrid(grid => grid?.revision === revision ? null : grid)
    })
    return () => controller.abort()
  }, [revision, artifactsCurrent, flattened?.id])
  const orderMatters = layerOrderMatters(traceSettings)
  const automaticOrderCached = Boolean(revision && automaticOrders.has(revision))
  useEffect(() => {
    // A revision's automatic order never changes, so it is read once per revision, and only
    // once the trace settings make layer order matter. A failed read is retried by the
    // list's Retry, or on the next revision or setting change that needs it.
    if (!revision || !flattened || !artifactsCurrent || !orderMatters || automaticOrderCached) return
    const controller = new AbortController()
    setAutomaticOrderError(failure => failure?.revision === revision ? null : failure)
    api.automaticLayerOrder(revision, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return
        setAutomaticOrders(orders => {
          const next = new Map(orders)
          next.delete(revision)
          next.set(revision, result.automatic_layer_order)
          while (next.size > AUTOMATIC_ORDER_CACHE) next.delete(next.keys().next().value!)
          return next
        })
      })
      .catch(cause => { if (!controller.signal.aborted) setAutomaticOrderError({ revision, message: String(cause) }) })
    return () => controller.abort()
  }, [revision, artifactsCurrent, flattened?.id, orderMatters, automaticOrderCached, automaticOrderAttempt])
  const automaticOrder = revision && automaticOrders.has(revision) ? automaticOrders.get(revision) ?? null : null
  // Until the automatic order is known the list would show palette order as "automatic",
  // so it stays locked: loading, the failure with a Retry, or a revision with no automatic
  // order (it cannot be traced as it stands, e.g. its cleanup is stale).
  const automaticOrderLock = !revision || !flattened || !orderMatters ? null
    : automaticOrderCached ? (automaticOrder ? null : 'Automatic order unavailable: this revision cannot be traced as it stands.')
      : automaticOrderError?.revision === revision ? `Automatic order unavailable: ${automaticOrderError.message}`
        : 'Loading automatic order…'
  useEffect(() => {
    // The Edit stage shows one overlay for whichever tool is active: the pencil's
    // staged (uncommitted) pixels in their target colours, or the selected areas.
    if (!editorPalette.length) { setPencilLabel(null); return }
    setPencilLabel(current => current !== null && current < editorPalette.length ? current : editorPalette[0].index)
  }, [editorPalette])
  useEffect(() => {
    // Build the active tool's overlay as a bounding-box patch and hand it to the canvas
    // layer, which blits just that rectangle — no full-image PNG encode per interaction.
    if (!flattenedPixels) { setOverlayFrame(undefined); return }
    const { width } = flattenedPixels
    const region = editTool === 'pencil'
      ? paintOverlayPixels(paintedCells, editorPalette, width)
      : overlayPixels(regionSelection)
    setOverlayFrame(region ?? undefined)
  }, [editTool, regionSelection, paintedCells, flattenedPixels, editorPalette])
  useEffect(() => {
    // Live palette recolour: repaint the flattened raster in the browser so dragging
    // the colour picker updates the canvas instantly, before anything is committed.
    // rAF coalesces the picker's rapid onChange stream to one encode per frame.
    const from = paletteDraft && editorPalette.find(colour => colour.index === paletteDraft.index)?.rgb
    if (!paletteDraft || !flattenedPixels || !from) { setPalettePreviewFrame(undefined); return }
    const { width, height } = flattenedPixels
    const to = hexToRgb(paletteDraft.hex)
    const handle = requestAnimationFrame(() => {
      // The whole-colour preview covers the entire raster, so the patch is the full image.
      const data = recolourPreviewPixels(flattenedPixels, from, to)
      setPalettePreviewFrame({ data, bounds: { x: 0, y: 0, width, height } })
    })
    return () => cancelAnimationFrame(handle)
  }, [paletteDraft, flattenedPixels, editorPalette])

  useEffect(() => {
    if (availableViews.length && !availableViews.includes(view)) setView(defaultView)
  }, [availableViews.join(','), view, defaultView])
  useEffect(() => {
    // Palette and Edit always show the editable image (and already switch to the
    // edited result themselves); moving to another revision abandons the landing too.
    if (!pendingView) return
    if (pendingView.revision !== revision || stage === 'palette' || stage === 'edit') { setPendingView(null); return }
    // The views on offer describe the shown artifacts; wait until they are the revision's.
    if (!artifactsCurrent) return
    if (availableViews.includes(pendingView.view)) { setView(pendingView.view); setPendingView(null) }
  }, [pendingView, revision, stage, availableViews.join(','), artifactsCurrent])
  useEffect(() => {
    if (stage === 'palette' || stage === 'edit') setView(editableView)
  }, [stage, editableView])
  useEffect(() => {
    if (selectionMode) setView('source')
  }, [selectionMode])
  // The raster the cleanup behind `revisionId` started from, when that cleanup wrote the
  // shown raster; the cached one when it is the same cleanup. A failed lookup resolves
  // to none, uncached, with the error the load reports when it commits (none clears it).
  async function beforeCleanupRaster(rows: Revision[], revisionId: string, shown: Artifact[], signal: AbortSignal): Promise<{ raster: typeof preCleanup; error: string | null }> {
    const lookup = nearestCleanup(rows, revisionId)
    const cleanup = lookup.stale ? null : lookup.revision
    if (!cleanup || shown.find(row => row.kind === 'flattened')?.revision_id !== cleanup.id) return { raster: null, error: null }
    if (preCleanupRef.current?.cleanupId === cleanup.id) return { raster: preCleanupRef.current, error: null }
    try {
      const before = (await api.effectiveArtifacts(cleanup.id, signal, { beforeCleanup: true })).find(row => row.kind === 'flattened')
      if (!before) throw new Error('no flattened raster precedes the cleanup')
      return { raster: { cleanupId: cleanup.id, flattened: before }, error: null }
    } catch (cause) {
      if (signal.aborted) throw cause
      return { raster: null, error: `Could not load the artwork from before the cleanup: ${String(cause)}` }
    }
  }
  // The flattened original the edits behind `revisionId` started from: the raster its
  // nearest palette revision wrote; the cached one for the same palette revision. A
  // failed lookup resolves to none, uncached, with the error the load reports when it
  // commits (none clears it).
  async function preEditRaster(rows: Revision[], revisionId: string, signal: AbortSignal): Promise<{ raster: typeof preEdit; error: string | null }> {
    const { hasEdit: edited, paletteAncestorId: paletteId } = editAncestry(rows, revisionId)
    if (!edited || !paletteId) return { raster: null, error: null }
    if (preEditRef.current?.paletteId === paletteId) return { raster: preEditRef.current, error: null }
    try {
      const original = (await api.artifacts(paletteId, signal)).find(row => row.kind === 'flattened')
      if (!original) throw new Error('the flatten wrote no flattened raster')
      return { raster: { paletteId, flattened: original }, error: null }
    } catch (cause) {
      if (signal.aborted) throw cause
      return { raster: null, error: `Could not load the flattened artwork from before the edits: ${String(cause)}` }
    }
  }

  async function deleteProject(id: string) {
    setError('')
    setDeleting(true)
    try {
      await api.deleteProject(id)
      localStorage.removeItem(draftKey(id))
      setProjects(current => current.filter(project => project.id !== id))
      // Leave the deleted project only if it is still the one shown; the selection at
      // the time of the click may have changed while the delete was in flight.
      if (stillSelected(id)) {
        setJob(null)
        selectProject(null)
        setRevision(null)
        requestAnimationFrame(() => document.querySelector<HTMLSelectElement>('header select.active-project')?.focus())
      }
    } catch (cause) { setError(String(cause)) }
    finally { setDeleting(false); setConfirmDelete(null) }
  }
  const selectedProjectName = projects.find(project => project.id === selected)?.name
  // A failure's message, naming the project it happened in once that is no longer shown.
  function projectFailure(projectId: string, projectName: string | undefined, action: string, cause: unknown) {
    return stillSelected(projectId) ? String(cause) : `${projectName ?? 'Another project'}: ${action}: ${String(cause)}`
  }

  async function importImage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    const file = (formElement.elements.namedItem('source') as HTMLInputElement).files?.[0]
    if (!file?.size) return setError('Choose an image to import')
    if (file.size > maxUploadBytes) return setError(
      `Image is ${formatMegabytes(file.size)}, over the ${formatMegabytes(maxUploadBytes)} upload limit. Reduce its size and try again.`)
    setUploading(true)
    setUploadFraction(0)
    try {
      // A completed transfer (fraction 1) means the bytes are sent and the server is
      // now saving; drop to indeterminate so the overlay reads as processing.
      const project = await api.importProject(String(form.get('name') || file.name.replace(/\.[^.]+$/, '')), file,
        fraction => setUploadFraction(fraction >= 1 ? null : fraction))
      setProjects(current => [project, ...current])
      // Hand straight to the project-load overlay in the same commit that clears
      // uploading, so the canvas never flashes uncovered between the two.
      setProjectLoading(true)
      selectProject(project.id)
      setRevision(project.revisions[0].id)
      formElement.reset()
    } catch (cause) { setError(String(cause)) }
    finally { setUploading(false); setUploadFraction(0) }
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
      selectProject(started.project.id)
      setJob(started.job)
    } catch (cause) { setError(String(cause)) }
  }

  // `rerunLastCleanup` re-runs the last (stale) cleanup with its own recorded settings,
  // which the server reads, naming its removed colour by hex (pipeline.rerun_cleanup_settings).
  async function runPipeline(kind: 'palette' | 'cleanup' | 'trace' | 'validate' | 'export', rerunLastCleanup = false) {
    if (!selected || !revision || starting.current) return
    // Starting lasts until the server has answered (the job then keeps `busy`), so a second
    // click in between never starts a second job.
    starting.current = true
    setStartingJob(true)
    setError('')
    try {
      const settings = kind === 'palette' ? { palette: paletteSettings }
        : kind === 'cleanup' ? (rerunLastCleanup ? { rerun_last_cleanup: true } : { cleanup: cleanupSettings })
          : kind === 'trace' ? { trace: traceSettingsForRun(revision) ?? await traceSettingsFetchingPalette(revision) } : undefined
      // A layer order the run leaves out (order cannot matter) is the form's own, whichever
      // run it came from: keep it in the draft, where the next form finds it, unless the
      // user's pending draft already names an order, which wins.
      const withheld = kind === 'trace' && !orderMatters ? traceSettings.layer_order : null
      if (withheld && !draftTraceOrder(selected)) keepTraceDraftOrder(selected, withheld)
      const draftOrder = withheld ? draftTraceOrder(selected) : null
      const started = await api.startPipeline(kind, selected, revision, settings)
      // Only a run from the newest trace carries the pending trace draft, so only that
      // run spends it; a branch from an older run leaves it for the newest.
      traceDraftSpent.current = kind === 'trace' && traceRuns.index <= 0 ? started.id : null
      traceOrderHeld.current = traceDraftSpent.current && draftOrder ? { jobId: started.id, order: draftOrder } : null
      cleanupDraftSpent.current = kind === 'cleanup' && !rerunLastCleanup ? started.id : null
      setJob(started)
    }
    catch (cause) { if (!(cause instanceof DOMException && cause.name === 'AbortError')) setError(String(cause)) }
    finally { starting.current = false; setStartingJob(false) }
  }
  useEffect(() => () => { startRead.current?.abort() }, [revision, selected])
  // The trace form for a run of `revisionId`, its layer order narrowed to that revision's
  // own palette so a stale order never runs; null when that palette must be read first.
  function traceSettingsForRun(revisionId: string): TraceSettings | null {
    if (!traceSettings.layer_order || !orderMatters) return withoutMootLayerOrder(traceSettings)
    return paletteRevision === revisionId ? withPaletteLayerOrder(traceSettings, editorPalette, removedIndex) : null
  }
  // Reading the palette is part of starting the job (the job buttons stay disabled), and a
  // revision or project change aborts it (the throw is then dropped).
  async function traceSettingsFetchingPalette(revisionId: string): Promise<TraceSettings> {
    const controller = new AbortController()
    startRead.current = controller
    try {
      const state = await api.editor(revisionId, controller.signal)
      if (controller.signal.aborted) throw new DOMException('The palette read was superseded.', 'AbortError')
      return withPaletteLayerOrder(traceSettings, state.palette, state.removed_colour_index ?? null)
    } finally {
      if (startRead.current === controller) startRead.current = null
    }
  }
  function persistDraft(step: 'palette' | 'cleanup' | 'trace') {
    // The trace draft belongs to the newest run; an older run's form is its record.
    if (!selected || (step === 'trace' && traceRuns.index > 0)) return
    // A palette draft's points belong to the source they were picked on, so it is kept
    // only with the revision that names that source.
    if (step === 'palette' && !current) return
    let draft: { palette?: PaletteSettings; palette_revision?: string; cleanup?: Partial<CleanupSettings>; trace?: TraceSettings } = {}
    try { draft = JSON.parse(localStorage.getItem(draftKey(selected)) || '{}') } catch { draft = {} }
    if (step === 'palette') { draft.palette = paletteSettings; draft.palette_revision = current!.id }
    else if (step === 'cleanup') {
      // Only the user's own choice of removed colour is a draft: a value equal to the
      // inherited one (which the server may have chosen, e.g. a current name) is left
      // out, so it is resolved afresh rather than frozen into the draft.
      const { remove_colour: removeColour, ...rest } = cleanupSettings
      draft.cleanup = removeColour === baselineCleanup.remove_colour ? rest : cleanupSettings
    }
    else {
      // The form's own settings supersede any pixel-era draft still waiting to convert.
      draft.trace = traceSettings
      pixelTrace.current = null
    }
    localStorage.setItem(draftKey(selected), JSON.stringify(draft))
  }
  // The layer order of the project's pending trace draft (undefined: the draft names none).
  function draftTraceOrder(projectId: string): string[] | null | undefined {
    try { return (JSON.parse(localStorage.getItem(draftKey(projectId)) || '{}') as { trace?: Partial<TraceSettings> }).trace?.layer_order } catch { return undefined }
  }
  // Set `order` as the layer order of the project's trace draft, keeping its other fields.
  function keepTraceDraftOrder(projectId: string, order: string[] | null) {
    let draft: { trace?: Partial<TraceSettings> } = {}
    try { draft = JSON.parse(localStorage.getItem(draftKey(projectId)) || '{}') } catch { draft = {} }
    draft.trace = { ...draft.trace, layer_order: order }
    localStorage.setItem(draftKey(projectId), JSON.stringify(draft))
  }
  function clearDraft(projectId: string, step: 'palette' | 'cleanup' | 'trace') {
    let draft: { palette?: PaletteSettings; palette_revision?: string; cleanup?: CleanupSettings; trace?: TraceSettings } = {}
    try { draft = JSON.parse(localStorage.getItem(draftKey(projectId)) || '{}') } catch { return }
    delete draft[step]
    if (step === 'palette') delete draft.palette_revision
    localStorage.setItem(draftKey(projectId), JSON.stringify(draft))
  }
  async function reimportSvg(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selected || !revision) return
    const formElement = event.currentTarget
    const form = new FormData(formElement)
    const file = form.get('svg') as File
    if (!file?.size) return setError('Choose an edited SVG to reimport')
    const projectId = selected, projectName = selectedProjectName
    try {
      const next = await api.reimportInkscape(projectId, revision, file)
      if (!stillSelected(projectId)) return
      setRevision(next.id); setHandoffMessage('Edited SVG validated and saved as a new revision.')
      formElement.reset()
    } catch (cause) { setError(projectFailure(projectId, projectName, 'could not reimport the edited SVG', cause)) }
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
  // A distinct identity per operation so the elapsed timer restarts at 0:00 whenever
  // a different operation takes the overlay, driven off a client-side clock (the only
  // mechanism that also covers upload, which has no job).
  const overlayOperation = projectLoading ? 'loading' : busy ? `job:${job?.id ?? ''}` : uploading ? 'upload' : null
  useEffect(() => {
    if (!overlayOperation) { setElapsedSeconds(0); return }
    const start = Date.now()
    setElapsedSeconds(0)
    const timer = setInterval(() => setElapsedSeconds(Math.floor((Date.now() - start) / 1000)), 1000)
    return () => clearInterval(timer)
  }, [overlayOperation])
  const workspaceLayers = artifacts.map(artifactToWorkspaceLayer).filter((layer): layer is WorkspaceLayer => layer !== null)
    .map(layer => TRACED_SVG_KINDS.has(artifacts.find(row => row.id === layer.id)?.kind ?? '') && flattened ? { ...layer, sizeFrom: flattened.id } : layer)
    .map(layer => layer.id === tracedSvg?.id ? { ...layer, name: tracedSvgName(tracedRunSettings, traceRunIsPhysical(revisions, tracedRunId)) } : layer)
  const preEditLayer: WorkspaceLayer | null = hasEdit && preEditFlattened ? {
    id: preEditFlattened.id, name: `flattened (original) · ${preEditFlattened.filename}`,
    url: preEditFlattened.download_url, mediaType: preEditFlattened.media_type, role: 'image',
  } : null
  // A cleanup run with cleanup off keeps its parent's raster, so the pre-cleanup layer is
  // only added when it is a different artifact.
  const preCleanupLayer: WorkspaceLayer | null = preCleanupFlattened && preCleanupFlattened.id !== flattened?.id
    && preCleanupFlattened.id !== preEditLayer?.id ? {
      id: preCleanupFlattened.id, name: `${hasEdit ? 'edited' : 'flattened'} (before cleanup) · ${preCleanupFlattened.filename}`,
      url: preCleanupFlattened.download_url, mediaType: preCleanupFlattened.media_type, role: 'image',
    } : null
  const preUpscaleLayer: WorkspaceLayer | null = preUpscaleSource && preUpscaleSource.id !== source?.id ? {
    id: preUpscaleSource.id, name: `source (before upscale) · ${preUpscaleSource.filename}`,
    url: preUpscaleSource.download_url, mediaType: preUpscaleSource.media_type, role: 'image',
  } : null
  const displayLayers = [...workspaceLayers, ...(preEditLayer ? [preEditLayer] : []), ...(preCleanupLayer ? [preCleanupLayer] : []), ...(preUpscaleLayer ? [preUpscaleLayer] : [])]
  // Never the cleaned raster: without its before-cleanup raster the views are unavailable.
  const beforeCleanupId = hasCleanup ? preCleanupFlattened?.id : flattened?.id
  const viewLayerId = (target: ArtworkView): string | undefined =>
    target === 'source' ? source?.id
      : target === 'traced' ? tracedSvg?.id
        : target === 'cleaned' ? flattened?.id
          : target === 'edited' ? beforeCleanupId
            : hasEdit ? preEditLayer?.id : beforeCleanupId
  // The size the next trace uses, from the active cleanup, completed from the raster.
  const traceSize = physicalSize(activeCleanup?.settings.cleanup as Partial<CleanupSettings> | undefined, tracedSize)

  const regionLabels = selectionLabels(regionSelection)
  const regionNeighbours = useMemo(
    () => flattenedPixels && regionSelection.length ? neighbourLabels(regionSelection, flattenedPixels, editorPalette) : [],
    [regionSelection, flattenedPixels, editorPalette])

  function toggleRegion(point: [number, number]) {
    if (!flattenedPixels || pixelsRevision !== revision) return
    setRegionSelection(selection => toggleComponent(selection, flattenedPixels, point[0], point[1], editorPalette))
  }

  async function applyRegionColour(label: number) {
    if (!selected || !revision || canvasComparing) return
    const submitted = regionSelection
    const operations = submitted
      .filter(component => component.label !== label)
      .map(component => ({ tool: 'reassign_region' as const, point: component.point, target_label: label }))
    if (operations.length === 0) return
    const projectId = selected
    setRegionBusyProjects(busy => [...busy, projectId]); setRegionError('')
    try {
      const reassigned = submitted.filter(component => component.label !== label)
      const next = await api.applyEdits(projectId, revision, operations)
      // An edit resolving after a project switch belongs to the project left behind:
      // its pixels, selection and revision must not land on the one now shown.
      if (!stillSelected(projectId)) return
      // Adopt the exact recoloured raster instead of re-decoding the flattened PNG — but
      // only when every palette colour is a distinct RGB. With a duplicate RGB an
      // RGB-flooded component can straddle two labels, whereas the server reassigns just
      // the clicked label's region, so the client-painted blob would diverge; fall back
      // to re-decoding the authoritative PNG in that case.
      const rgb = editorPalette.find(colour => colour.index === label)?.rgb
      if (rgb && flattenedPixels && !paletteHasDuplicateRgb(editorPalette)) patchedFlattened.current = {
        revision: next.id, pixels: pixelsWithComponents(flattenedPixels, reassigned, rgb) }
      // Deselect only the submitted batch; areas clicked after kickoff are new
      // components and are handed to the revision reload so they stay selected. The
      // reload effect clears the selection otherwise, so we route through it rather
      // than setting selection here (which it would immediately overwrite).
      preserveRegion.current = { id: next.id, selection: removeComponents(regionSelectionRef.current, submitted), applied: false }
      setRevision(next.id)
    } catch (cause) { if (stillSelected(projectId)) setRegionError(String(cause)) }
    finally { setRegionBusyProjects(busy => busy.filter(id => id !== projectId)) }
  }

  // A revision a child editor committed in `projectId`, applied only while that project is
  // still selected: a palette edit resolving after a switch belongs to the project left.
  function revisionIn(projectId: string) {
    return (id: string) => { if (stillSelected(projectId)) setRevision(id) }
  }

  function undoRegion() {
    if (current?.parent_revision_id) setRevision(current.parent_revision_id)
  }

  function chooseEditTool(tool: 'areas' | 'pencil') {
    setEditTool(tool)
    // Switching tools drops the other tool's uncommitted staging so the overlay
    // never shows a selection the active tool cannot act on.
    if (tool === 'pencil') setRegionSelection([]); else setPaintedCells(new Map())
  }

  function paintPixel(x: number, y: number) {
    if (pencilLabel === null || !flattenedPixels || pixelsRevision !== revision) return
    const cell = y * flattenedPixels.width + x
    setPaintedCells(prev => prev.get(cell) === pencilLabel ? prev : new Map(prev).set(cell, pencilLabel))
  }

  async function commitPencil() {
    if (!selected || !revision || !flattenedPixels || paintedCells.size === 0 || canvasComparing) return
    const { width } = flattenedPixels
    const byLabel = new Map<number, [number, number][]>()
    for (const [cell, label] of paintedCells) {
      const x = cell % width, y = (cell - x) / width
      const pixels = byLabel.get(label) ?? []
      pixels.push([x, y]); byLabel.set(label, pixels)
    }
    const operations = [...byLabel].map(([label, pixels]) => ({ tool: 'paint_pixels' as const, target_label: label, pixels }))
    const projectId = selected
    setPencilBusyProjects(busy => [...busy, projectId]); setPencilError('')
    try {
      const next = await api.applyEdits(projectId, revision, operations)
      // Dropped after a project switch, exactly as an area edit is.
      if (!stillSelected(projectId)) return
      // Adopt the exact painted raster instead of re-decoding the flattened PNG.
      patchedFlattened.current = {
        revision: next.id, pixels: pixelsWithPaintedCells(flattenedPixels, paintedCells, editorPalette) }
      setPaintedCells(new Map())
      setRevision(next.id)
    } catch (cause) { if (stillSelected(projectId)) setPencilError(String(cause)) }
    finally { setPencilBusyProjects(busy => busy.filter(id => id !== projectId)) }
  }

  // A preservation point is being picked on the shown source image (Edit's own canvas
  // clicks select areas instead); only then does the canvas aim the pick.
  const preservationPicking = stage !== 'edit' && Boolean(selectionMode) && view === 'source'
  const pointLists = JSON.stringify([paletteSettings.protected_component_points, paletteSettings.preserved_colour_points, paletteSettings.distinct_colour_point_pairs])

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
    <header><div className="mobile-brand"><span className="mark" aria-hidden="true">V</span><h1>Vinylgen</h1></div>{stage !== 'upload' && viewToggle()}<select className="active-project" aria-label="Active project" title={selectedProjectName ?? 'No project selected'} value={selected ?? ''} onChange={event => selectProject(event.target.value || null)}><option value="">No project selected</option>{projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}</select>{selected && <button type="button" className="delete-project" aria-label="Delete project" title="Delete project" onClick={() => setConfirmDelete({ id: selected, name: selectedProjectName ?? 'this project' })}><Trash2 aria-hidden="true" /></button>}</header>
    <div className="workflow-sidebar">
      <div className="sidebar-brand"><span className="mark" aria-hidden="true">V</span><div><h1>Vinylgen</h1></div></div>
      <nav className="workflow-nav" aria-label="Artwork workflow" role="tablist" aria-orientation={desktopWorkflow ? 'vertical' : 'horizontal'}>
        {WORKFLOW_STAGES.map(item => <button key={item.id} id={`workflow-tab-${item.id}`} type="button" role="tab" aria-label={item.label} data-stage-button={item.id} aria-selected={stage === item.id} aria-controls={`workflow-panel-${item.id}`} tabIndex={stage === item.id ? 0 : -1} onKeyDown={event => navigateStageByKey(event, item.id)} onClick={() => navigateStage(item.id)}><item.icon aria-hidden="true"/><span><strong>{item.label}</strong><small>{item.description}</small></span>{(item.id === 'flatten' || item.id === 'cleanup' || item.id === 'trace') && busy && <i className="stage-running" aria-label="Job running" />}</button>)}
      </nav>
    </div>
    <p className="visually-hidden" aria-live="polite" aria-atomic="true">{WORKFLOW_STAGES.find(item => item.id === stage)?.label} stage</p>
    <section className="mobile-context" aria-label="Current work"><div className="mobile-context-head"><strong>{WORKFLOW_STAGES.find(item => item.id === stage)?.label}</strong><span>{projects.find(project => project.id === selected)?.name || 'No project selected'}</span></div>{artworkStageActive && availableViews.length > 1 && <div className="mobile-view-switch">{viewToggle()}</div>}</section>
    {error && <p className="error global-error" role="alert">{error}</p>}
    {error && error === loadError && <button type="button" className="secondary global-retry" onClick={retryLoad}>Retry loading</button>}
    {job && !busy && !projectLoading && !uploading && <p className={job.status === 'failed' ? 'error job-result' : 'success job-result'} role={job.status === 'failed' ? 'alert' : 'status'} aria-live={job.status === 'failed' ? 'assertive' : 'polite'} aria-atomic="true">{jobMessage}</p>}
    <section className={`workspace workflow-${stage}`}>
      <div className={`workflow-surface artwork-surface ${projectLoading || busy || uploading ? 'is-loading' : ''} ${['flatten', 'cleanup', 'trace', 'palette', 'edit'].includes(stage) || selectionMode || busy || projectLoading || uploading ? 'mobile-stage-active' : ''}`} data-stage="artwork">
        {stage === 'upload' || stage === 'flatten' || stage === 'palette' || stage === 'edit' || stage === 'cleanup' || stage === 'trace' || selectionMode ? <ImageWorkspace
          layers={displayLayers} editorMode
          canvasLabel={stage === 'upload' ? 'Upload canvas' : stage === 'flatten' ? 'Flatten canvas' : stage === 'trace' && !selectionMode ? 'Trace canvas' : stage === 'cleanup' && !selectionMode ? 'Cleanup canvas' : 'Palette canvas'}
          inspectionKinds={canvasOverlayKinds(stage, Boolean(selectionMode))}
          // The Trace canvas marks the path nodes of the traced SVG it shows (the variant
          // the run's settings chose), types read from its sodipodi:nodetypes.
          pathNodes={stage === 'trace' && !selectionMode && tracedSvg ? { url: tracedSvg.download_url } : undefined}
          // Preservation points are source pixels; the canvas scales them onto a larger
          // flattened raster. They are mapped onto the selected revision's source, so they
          // wait until the shown artifacts are that revision's own.
          markers={(stage === 'flatten' || selectionMode) && artifactsCurrent ? preservationMarkers(paletteSettings, selectionMode === 'distinct-second' ? distinctFirst : null) : undefined}
          markerGridFrom={source?.id}
          highlightedMarker={highlight?.lists === pointLists ? highlight.id : null}
          livePreview={stage === 'trace' && !selectionMode && revision ? { revisionId: revision, settings: withoutMootLayerOrder(withPaletteLayerOrder(traceSettings, paletteRevision === revision ? editorPalette : [], removedIndex)), tracedSize } : undefined}
          // The unsaved Cleanup form, physical size included; nothing is saved.
          // A cleanup that changed pixels but has no changes overlay ran before runs recorded one.
          cleanupPreview={stage === 'cleanup' && !selectionMode && revision ? { revisionId: revision, settings: cleanupSettings, tracedSize, changesUnavailable: hasCleanup ? 'Re-run Cleanup to record its changes' : 'No Cleanup changes to show' } : undefined}
          // Paused until the selected revision's own traced grid is known, so a preview is
          // never requested with another revision's size.
          livePreviewPaused={!artifactsCurrent || tracedGrid?.revision !== revision || (stage === 'cleanup' && Boolean(removeBlocked))}
          livePreviewStatus={stage === 'cleanup' && removeBlocked ? removeBlocked : failedRevision && failedRevision === revision ? 'Preview unavailable: the selected revision failed to load.'
            : tracedGridError && tracedGridError.revision === revision && tracedGrid?.revision !== revision ? `Preview unavailable: ${tracedGridError.message}` : undefined}
          // Loading from the moment the revision changes, not only once the fetch starts.
          loading={revisionLoading || (Boolean(artifactsRevision) && !artifactsCurrent && failedRevision !== revision)}
          // Upload always shows the source, the image it imports (or upscales).
          preferredLayerId={stage === 'upload' ? source?.id : viewLayerId(view)}
          compareLineage={preUpscaleLayer && source ? { id: preUpscaleLayer.id, upscaledId: source.id, before: upscaleBefore ?? undefined, after: upscaleAfter ?? undefined } : undefined}
          // Palette and Edit open with Compare Off (comparing there locks editing); the
          // other canvases open in the remembered mode.
          compareRemembered={stage !== 'palette' && stage !== 'edit'}
          compareKey={stage}
          compareLocksEditing={stage === 'palette' || stage === 'edit'}
          onComparingChange={setCanvasComparing}
          compareStatus={preUpscaleFailure ? { message: preUpscaleFailure, onRetry: () => setOriginalSourceAttempt(attempt => attempt + 1) } : undefined}
          compareBefore={canvasCompareBefore(stage, view, Boolean(selectionMode), {
            originalSource: preUpscaleLayer?.id, source: source?.id, preEdit: preEditLayer?.id, beforeCleanup: preCleanupLayer?.id,
            raster: flattened?.id, all: displayLayers.filter(layer => layer.role === 'image').map(layer => layer.id) })}
          selectionLabel={stage === 'edit' ? (editTool === 'pencil' ? 'Flattened artwork pencil' : 'Flattened artwork area selector') : selectionMode ? 'Source image preservation-point selector' : undefined}
          onSelectPoint={stage === 'edit' ? (editSelectable && editTool === 'areas' ? toggleRegion : undefined) : (preservationPicking ? selectPreservationPoint : undefined)}
          pickAim={preservationPicking}
          paintMode={stage === 'edit' && editTool === 'pencil' && editSelectable}
          onPaint={stage === 'edit' && editTool === 'pencil' && editSelectable ? paintPixel : undefined}
          overlay={stage === 'palette' && editSelectable ? palettePreviewFrame : stage === 'edit' && editSelectable ? overlayFrame : undefined}
        /> : <ArtworkPreview layers={displayLayers} preferredLayerId={viewLayerId(view)} />}
        {projectLoading
          ? <LoadingOverlay title="Loading artwork" message="Fetching the selected revision and its artifacts…" progress={null} elapsedSeconds={elapsedSeconds} progressLabel="Loading progress" />
          : busy
            ? <LoadingOverlay title="Working" message={`${jobMessage}. This can take a while; Vinylgen is still working.`} progress={jobProgress} elapsedSeconds={elapsedSeconds} progressLabel="Job progress" onCancel={() => api.cancelJob(job!.id).then(setJob).catch(cause => setError(String(cause)))} />
            : uploading
              ? <LoadingOverlay title="Uploading" message={uploadFraction !== null ? `Uploading image… ${Math.round(uploadFraction * 100)}%` : 'Processing image…'} progress={uploadFraction} elapsedSeconds={elapsedSeconds} progressLabel="Upload progress" />
              : null}
      </div>
      <aside>
        <section id="workflow-panel-create" role="tabpanel" aria-labelledby="workflow-tab-create" className={`side-section create-section workflow-surface ${stage === 'create' ? 'mobile-stage-active' : ''}`} data-stage="create">
        <h2>Create artwork</h2>
        <form className="generation-form" key={sourceRevision?.id ?? 'new-source'} onSubmit={generateImage}>
          <label>Short subject<textarea name="prompt" placeholder="e.g. sky whale city" defaultValue={generation?.short_prompt ?? ''} ref={element => { subjectField.current = element; growToFit(element) }} onFocus={event => growToFit(event.currentTarget)} onInput={event => growToFit(event.currentTarget)} required /></label>
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
          <label className="file">Source image<input name="source" type="file" accept="image/*" required /><small>Choose the raster that will become the immutable source revision. Up to {formatMegabytes(maxUploadBytes)}.</small></label>
          <button type="submit">Import image</button>
        </form>
        </section>
        <section id="workflow-panel-flatten" role="tabpanel" aria-labelledby="workflow-tab-flatten" className={`side-section pipeline-section workflow-surface ${stage === 'flatten' ? 'mobile-stage-active' : ''}`} data-stage="flatten">
        <div className="section-heading"><h2>Flatten colours</h2><span>{revision ? 'Ready' : 'Choose a project'}</span></div><p className="section-intro">Turn the source into an exact limited-colour raster. Review preservation exceptions before running.</p>
        <div className="job">
          {!revision && <p className="stage-guidance">Choose an active project in the header first.</p>}
          <button disabled={!revision || busy || startingJob || Boolean(selectedUnavailable)} aria-describedby={lockedBy('flatten-lock-reason')} onClick={() => runPipeline('palette')}>Flatten image</button>
          {sourceRevision && <button type="button" className="secondary" disabled={busy || Boolean(selectedUnavailable)} aria-describedby={lockedBy('flatten-lock-reason')} onClick={() => setUpscaleOpen(true)}>Upscale source…</button>}
          {lockReason('flatten-lock-reason')}
        </div>
        <PipelineControls stage="flatten" palette={paletteSettings} setPalette={setPaletteSettings} basePalette={baselinePalette} trace={traceSettings} setTrace={setTraceSettings} baseTrace={baselineTrace} paletteColours={editorPalette}
          selectionMode={selectionMode} setSelectionMode={setSelectionMode} onHighlightPoint={id => setHighlight(id === null ? null : { id, lists: pointLists })}
          onReset={() => { setPaletteSettings(DEFAULT_PALETTE_SETTINGS); setSelectionMode(null) }}
          iterationCount={paletteIterations.length} iterationIndex={paletteStep} onStep={stepPalette} onCommitDraft={() => persistDraft('palette')} />
        </section>
        <section id="workflow-panel-cleanup" role="tabpanel" aria-labelledby="workflow-tab-cleanup" className={`side-section pipeline-section workflow-surface ${stage === 'cleanup' ? 'mobile-stage-active' : ''}`} data-stage="cleanup">
          <div className="section-heading"><h2>Clean up for cutting</h2><span>{readiness}</span></div><p className="section-intro">Set the physical size and removed colour the trace uses, and remove or widen details too small to cut.</p>
          <div className="job"><button disabled={!revision || !selectedHasSegmentation || busy || startingJob || Boolean(cleanupRunBlocked)} aria-describedby={[lockedBy('cleanup-lock-reason'), cleanupRunBlocked ? 'cleanup-remove-reason' : undefined].filter(Boolean).join(' ') || undefined} onClick={() => runPipeline('cleanup')}>Run cleanup</button>{lockReason('cleanup-lock-reason')}{cleanupRunBlocked && <p id="cleanup-remove-reason" className="stage-guidance lock-reason">{cleanupRunBlocked}</p>}</div>
          <PipelineControls stage="cleanup" palette={paletteSettings} setPalette={setPaletteSettings} basePalette={baselinePalette} trace={traceSettings} setTrace={setTraceSettings} baseTrace={baselineTrace} cleanup={cleanupSettings} setCleanup={setCleanupSettings} baseCleanup={baselineCleanup} paletteColours={editorPalette} removalPaletteCurrent={paletteRevision === revision} removalBlocked={removeBlocked} selectionMode={selectionMode} setSelectionMode={setSelectionMode} onReset={() => setCleanupSettings(DEFAULT_CLEANUP_SETTINGS)} iterationCount={cleanupIterations.length} iterationIndex={cleanupStep} onStep={stepCleanup} onCommitDraft={() => persistDraft('cleanup')} />
        </section>
        <section id="workflow-panel-trace" role="tabpanel" aria-labelledby="workflow-tab-trace" className={`side-section pipeline-section workflow-surface ${stage === 'trace' ? 'mobile-stage-active' : ''}`} data-stage="trace">
          <div className="section-heading"><h2>Trace vinyl layers</h2><span>{readiness}</span></div><p className="section-intro">Convert flattened colour regions into shared-boundary SVG layers.</p>
          {cleanupStale && <div className="stage-guidance cleanup-stale" role="status"><p>The artwork changed after Cleanup: re-run Cleanup</p><button type="button" className="secondary" title="Runs Cleanup again on this artwork with the settings of the last Cleanup run, not the Cleanup form" disabled={!revision || busy || startingJob || Boolean(selectedUnavailable)} onClick={() => runPipeline('cleanup', true)}>Re-run Cleanup with its last settings</button></div>}
          <div className="job"><button disabled={!revision || !selectedHasSegmentation || busy || startingJob || cleanupStale} aria-describedby={lockedBy('trace-lock-reason')} onClick={() => runPipeline('trace')}>Trace and validate</button>{lockReason('trace-lock-reason')}</div>
          <PipelineControls stage="trace" removedIndex={removedIndex} automaticOrder={automaticOrder} layerOrderLocked={!revision || paletteRevision === revision || (artifactsCurrent && !flattened) ? automaticOrderLock
            : tracedGridError && tracedGridError.revision === revision ? `Palette unavailable: ${tracedGridError.message}`
              : selectedUnavailable ?? 'Loading the selected revision…'} onRetryLayerOrder={automaticOrderLock && automaticOrderError?.revision === revision ? () => setAutomaticOrderAttempt(attempt => attempt + 1) : null} palette={paletteSettings} setPalette={setPaletteSettings} basePalette={baselinePalette} trace={traceSettings} setTrace={setTraceSettings} baseTrace={baselineTrace} paletteColours={editorPalette} selectionMode={selectionMode} setSelectionMode={setSelectionMode} onReset={() => setTraceSettings(DEFAULT_TRACE_SETTINGS)} iterationCount={traceRuns.runs.length} iterationIndex={Math.max(0, traceRuns.index)} onStep={stepTrace} stepDisabled={busy} onCommitDraft={() => persistDraft('trace')} tracedSize={tracedSize} traceSize={traceSize} />
        </section>
        <section id="workflow-panel-palette" role="tabpanel" aria-labelledby="workflow-tab-palette" className={`side-section edit-section workflow-surface ${stage === 'palette' ? 'mobile-stage-active' : ''}`} data-stage="palette">
          {(!selected || !revision || !source) && <><h2>Editing unavailable</h2><p className="stage-guidance">Create or choose artwork first.</p></>}
          {selected && revision && flattened && <>
            <nav className="history" aria-label="Revision history"><button disabled={!current?.parent_revision_id} onClick={() => current?.parent_revision_id && setRevision(current.parent_revision_id)}>Undo</button><select aria-label="Revision" value={revision} onChange={event => setRevision(event.target.value)}>{revisions.map(row => <option key={row.id} value={row.id}>{row.kind} · {new Date(row.created_at).toLocaleString()}</option>)}</select><button disabled={!redo} onClick={() => redo && setRevision(redo.id)}>Redo</button></nav>
            <FlattenedEditor projectId={selected} revisionId={revision} flattened={flattened} onCommit={revisionIn(selected)} onPreview={(index, hex) => setPaletteDraft({ index, hex })} onPreviewEnd={() => setPaletteDraft(null)} disabled={!editSelectable || Boolean(selectedUnavailable)} disabledHint={selectedUnavailable ?? `Switch to the ${editableLabel} image to edit its palette.`} lockedHint={canvasComparing ? COMPARE_EDIT_LOCK : undefined} />
          </>}
          {selected && revision && source && !flattened && <><h2>Palette unavailable</h2><p className="stage-guidance">Flatten this revision first to edit its palette.</p></>}
        </section>
        <section id="workflow-panel-edit" role="tabpanel" aria-labelledby="workflow-tab-edit" className={`side-section region-edit-section workflow-surface ${stage === 'edit' ? 'mobile-stage-active' : ''}`} data-stage="edit">
          {!selected || !revision || !flattened ? <><h2>Edit selected areas</h2><p className="stage-guidance">Flatten the active project before editing individual areas.</p></> : <>
            <div className="segmented edit-tool-toggle" role="group" aria-label="Edit tool">
              <button type="button" className={editTool === 'areas' ? 'active' : ''} aria-pressed={editTool === 'areas'} onClick={() => chooseEditTool('areas')}>Select areas</button>
              <button type="button" className={editTool === 'pencil' ? 'active' : ''} aria-pressed={editTool === 'pencil'} onClick={() => chooseEditTool('pencil')}>Pencil</button>
            </div>
            {editTool === 'areas'
              ? <RegionEditor palette={editorPalette} selectionLabels={regionLabels} neighbourLabels={regionNeighbours} onPick={applyRegionColour} onClear={() => setRegionSelection([])} onUndo={undoRegion} canUndo={Boolean(current?.parent_revision_id)} busy={regionBusy} error={regionError} disabled={!editSelectable || pixelsRevision !== revision} disabledHint={!editSelectable ? `Switch to the ${editableLabel} image to select and edit areas.` : selectedUnavailable ?? 'Loading the artwork…'} lockedHint={canvasComparing ? COMPARE_EDIT_LOCK : undefined} />
              : <PencilEditor palette={editorPalette} activeLabel={pencilLabel} onPickColour={setPencilLabel} paintedCount={paintedCells.size} onApply={commitPencil} onDiscard={() => setPaintedCells(new Map())} onUndo={undoRegion} canUndo={Boolean(current?.parent_revision_id)} busy={pencilBusy} error={pencilError} disabled={!editSelectable || pixelsRevision !== revision} disabledHint={!editSelectable ? `Switch to the ${editableLabel} image to paint pixels.` : selectedUnavailable ?? 'Loading the artwork…'} lockedHint={canvasComparing ? COMPARE_EDIT_LOCK : undefined} />}
          </>}
        </section>
      </aside>
    </section>
    <section id="workflow-panel-export" role="tabpanel" aria-labelledby="workflow-tab-export" className={`workflow-surface export-surface ${stage === 'export' ? 'mobile-stage-active' : ''}`} data-stage="export">
      {!handoff && <section className="mobile-stage-state"><h2>Export unavailable</h2><p>Complete Trace to create validated, exportable SVG layers.</p></section>}
      {selected && revision && handoff && <section className="inkscape-handoff"><h2>Export or finish in Inkscape</h2><p>{handoff.guidance}</p><div className="handoff-actions">{handoff.exports.map(item => <span key={item.id}><a className="button-link" href={item.download_url}>Download {item.filename}</a>{handoff.shared_folder_enabled && <button className="secondary" onClick={() => { const projectId = selected, projectName = selectedProjectName; api.saveForInkscape(revision, item.id).then(saved => { if (stillSelected(projectId)) setHandoffMessage(`Saved ${saved.filename} to the shared folder.`) }).catch(cause => setError(projectFailure(projectId, projectName, `could not save ${item.filename} to the shared folder`, cause))) }}>Save {item.media_type === 'image/png' ? 'PNG' : 'SVG'} to shared folder</button>}</span>)}</div><form onSubmit={reimportSvg}><label className="file">Edited SVG<input name="svg" type="file" accept="image/svg+xml,.svg" required /></label><button type="submit">Validate and reimport</button></form>{handoffMessage && <p className="success" role="status" aria-live="polite">{handoffMessage}</p>}</section>}
    </section>
    <ConfirmDialog open={Boolean(confirmDelete)} title="Delete project?" message={`Delete "${confirmDelete?.name}"? This permanently removes its revisions and artwork and cannot be undone.`} confirmLabel="Delete project" busy={deleting} onConfirm={() => confirmDelete && deleteProject(confirmDelete.id)} onCancel={() => setConfirmDelete(null)} />
    {selected && sourceRevision && <UpscalePanel open={upscaleOpen} projectId={selected} revisionId={sourceRevision.id} onClose={() => setUpscaleOpen(false)} onStarted={started => { setJob(started); setUpscaleOpen(false) }} />}
  </main>
}
