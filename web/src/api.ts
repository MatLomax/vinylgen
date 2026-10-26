export type Project = { id: string; slug: string; name: string; created_at: string; updated_at: string }
export type Revision = { id: string; project_id: string; parent_revision_id: string | null; kind: string; message: string | null; settings: Record<string, unknown>; created_at: string }
export type Artifact = { id: string; revision_id: string; kind: string; filename: string; media_type: string; byte_size: number; download_url: string }
export type Job = { id: string; project_id: string | null; revision_id: string | null; result_revision_id: string | null; kind: string; status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'; progress: number; message: string | null; error: string | null }
export type PaletteColour = { index: number; name: string; hex: string; rgb: [number, number, number]; share_percent: number }
export type EditorState = { width: number; height: number; palette: PaletteColour[]; requested_colour_count: number | null; protected_pixels: number; removed_colour_index?: number | null; inherited_remove_colour?: { value: string | null; unresolved: boolean; hex?: string | null; problem?: 'missing' | 'ambiguous' | null } | null }
// The bottom-to-top hex order a trace infers before any override (null: not traceable).
// Read on its own, and only when layer order can matter (layerOrderMatters).
export type AutomaticLayerOrder = { automatic_layer_order: string[] | null }
export type EditOperation = { tool: 'merge_colour' | 'rename_colour' | 'recolour_colour' | 'reassign_region' | 'paint_pixels'; label?: number; source_label?: number; target_label?: number; name?: string; rgb?: [number, number, number]; point?: [number, number]; pixels?: [number, number][] }
export type PaletteSettings = {
  initial_colours: number; final_colours: number | null; histogram_bits: number; analysis_max_dimension: number; dither_radius: number
  similar_delta_e: number; small_share: number; small_delta_e: number; fold_small_colours: boolean
  fragmented_share: number; fragmented_largest_region: number; fragmented_delta_e: number; merge_fragmented_colours: boolean
  min_output_colours: number; min_region_pixels: number; min_feature_width: number; min_colour_share: number
  intentional_line_min_contrast: number; intentional_line_min_length: number; protect_intentional_lines: boolean
  aa_ambiguity_margin: number; aa_smoothness: number; aa_resolution_passes: number; resolve_antialiasing: boolean; flatten_upscale: number; flatten_smooth_edges: boolean
  protected_component_points: [number, number][]; preserved_colour_points: [number, number][]
  distinct_colour_point_pairs: [[number, number], [number, number]][]
}
// The cleanup stage owns the physical size and the removed colour, which the trace
// then uses, as well as the cutter cleanup of the flattened labels.
export type CleanupSettings = {
  width_mm: number | null; height_mm: number | null; remove_colour: string | null
  cleanup: boolean; cleanup_action: 'remove' | 'widen' | 'remove_then_widen'
  min_detail_width: number; min_region_area: number; remove_below_width: number
}
export type TraceSettings = {
  overlap: number; solid_underlayers: boolean; layer_order: string[] | null
  // The unit of the eight trace lengths: thousandths of the traced image's longer side.
  // Records from before units carry none, or 'px' (see pixelRecordInUnits).
  length_unit: 'per_mille'; curve_fit_tolerance: number
  straight_line_max_deviation: number; minimum_straight_length: number
  max_deviation: number | null; corner_analysis_span: number; side_smoothness: number
  // Null (automatic) follows max_deviation, itself automatic (1‰) when that is null too.
  corner_sharpening_allowance: number | null
  straight_line_snap: 'off' | 'axis' | 'axis_diagonal'; snap_angle: number
  node_placement: 'prefer_extremes' | 'at_extremes' | 'fewest'; nodes_at_bends: boolean; curve_feature_scale: number
  soft_path_nodes: number; soft_layer_nodes: number; soft_document_nodes: number
}
export type GenerationSettings = { width: number; height: number; desired_colours: number; required_motifs: string[]; rejected_motifs: string[] }
export type GenerationStarted = { project: Project; job: Job; expanded_prompt: string }
export type SelectOption = { value: string; label: string }
// Pre-flatten generative tiled-detail upscale (see docs/upscale-stage.md section 5); distinct
// from the deterministic post-flatten `flatten_upscale` factor in PaletteSettings.
export type UpscaleSettings = {
  upscaler: string; upscale_factor: number; checkpoint: string; denoise: number
  controlnet: boolean; controlnet_strength: number; sample_size: number; tile_size: number; seed: number | null
}
// `available: false` means ComfyUI could not be reached, so no settings are offered.
export type UpscaleOptions =
  | { available: true; upscalers: SelectOption[]; checkpoints: SelectOption[]; controlnets: SelectOption[]; defaults: UpscaleSettings; sample_size_max: number }
  | { available: false }
export type UpscaleStarted = { project: Project; job: Job }
export type Health = { status: 'ok'; max_upload_bytes: number; active_jobs?: number }
export type InkscapeHandoff = { exports: Artifact[]; shared_folder_enabled: boolean; guidance: string }

type ValidationIssue = { loc?: unknown[]; msg?: unknown }

function describeDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail.trim() || null
  if (Array.isArray(detail)) {
    // FastAPI's request validation list: `{loc: ['body', 'settings', 'denoise'], msg}`.
    const issues = detail.map(item => {
      const { loc, msg } = (item ?? {}) as ValidationIssue
      if (typeof msg !== 'string') return null
      const path = Array.isArray(loc) ? loc.filter((part, index) => !(index === 0 && ['body', 'query', 'path'].includes(String(part)))).join('.') : ''
      return path ? `${path}: ${msg}` : msg
    }).filter((issue): issue is string => Boolean(issue))
    return issues.length ? issues.join('; ') : null
  }
  return null
}

// The message for a failed response: a JSON `detail` when the server sent one, otherwise a
// short status line. A non-JSON body (a proxy's HTML error page, plain text) is never shown.
export function errorMessage(status: number, statusText: string, body: string): string {
  try {
    const parsed = JSON.parse(body) as unknown
    if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
      const detail = describeDetail((parsed as { detail: unknown }).detail)
      if (detail) return detail
    }
  } catch { /* not JSON */ }
  return statusText ? `Server error ${status} (${statusText})` : `Server error ${status}`
}

async function failure(response: Response): Promise<Error> {
  const body = await response.text().catch(() => '')
  return new Error(errorMessage(response.status, response.statusText, body))
}

async function result<T>(response: Response): Promise<T> {
  if (!response.ok) throw await failure(response)
  return response.json() as Promise<T>
}

export const api = {
  health: () => fetch('/api/health').then(result<Health>),
  projects: () => fetch('/api/projects').then(result<Project[]>),
  project: (projectId: string, signal?: AbortSignal) => fetch(`/api/projects/${projectId}`, { signal }).then(result<Project & { revisions: Revision[] }>),
  deleteProject: (projectId: string) => fetch(`/api/projects/${projectId}`, { method: 'DELETE' }).then(async response => { if (!response.ok) throw await failure(response) }),
  artifacts: (revisionId: string, signal?: AbortSignal) => fetch(`/api/revisions/${revisionId}/artifacts`, { signal }).then(result<Artifact[]>),
  // `beforeCleanup` resolves the raster the cleanup stage starts from (see Store.effective_artifacts).
  effectiveArtifacts: (revisionId: string, signal?: AbortSignal, options?: { beforeCleanup?: boolean }) =>
    fetch(`/api/revisions/${revisionId}/effective-artifacts${options?.beforeCleanup ? '?before_cleanup=true' : ''}`, { signal }).then(result<Artifact[]>),
  editor: (revisionId: string, signal?: AbortSignal) => fetch(`/api/revisions/${revisionId}/editor`, { signal }).then(result<EditorState>),
  automaticLayerOrder: (revisionId: string, signal?: AbortSignal) => fetch(`/api/revisions/${revisionId}/automatic-layer-order`, { signal }).then(result<AutomaticLayerOrder>),
  applyEdits: (projectId: string, revisionId: string, operations: EditOperation[]) => fetch(`/api/projects/${projectId}/revisions/${revisionId}/edits`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ operations }),
  }).then(result<Revision>),
  importProject: (name: string, file: File, onProgress?: (fraction: number) => void) => {
    const body = new FormData()
    body.append('name', name)
    body.append('source', file)
    // XHR (not fetch) so the request exposes upload transfer progress; error and
    // success semantics mirror the shared result<T> helper.
    return new Promise<Project & { revisions: Revision[] }>((resolve, reject) => {
      const request = new XMLHttpRequest()
      request.open('POST', '/api/projects/import')
      request.upload.onprogress = event => { if (onProgress && event.lengthComputable) onProgress(event.loaded / event.total) }
      request.onload = () => {
        if (request.status >= 200 && request.status < 300) {
          try { resolve(JSON.parse(request.responseText) as Project & { revisions: Revision[] }) }
          catch { reject(new Error('Malformed import response')) }
        } else reject(new Error(errorMessage(request.status, request.statusText, request.responseText)))
      }
      request.onerror = () => reject(new Error(request.statusText || 'Upload failed'))
      request.send(body)
    })
  },
  generate: (prompt: string, projectName: string, settings: GenerationSettings) => fetch('/api/generations', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, project_name: projectName || null, provider: 'codex', settings }),
  }).then(result<GenerationStarted>),
  inkscapeHandoff: (revisionId: string, signal?: AbortSignal) => fetch(`/api/revisions/${revisionId}/inkscape-handoff`, { signal }).then(result<InkscapeHandoff>),
  saveForInkscape: (revisionId: string, artifactId: string) => {
    const body = new FormData(); body.append('artifact_id', artifactId)
    return fetch(`/api/revisions/${revisionId}/inkscape-save`, { method: 'POST', body }).then(result<{ filename: string; shared_path: string }>)
  },
  reimportInkscape: (projectId: string, revisionId: string, file: File) => {
    const body = new FormData(); body.append('svg', file)
    return fetch(`/api/projects/${projectId}/revisions/${revisionId}/inkscape-reimport`, { method: 'POST', body }).then(result<Revision>)
  },
  startDemo: (projectId: string) => fetch('/api/jobs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind: 'foundation-demo', project_id: projectId }),
  }).then(result<Job>),
  startPipeline: (kind: 'palette' | 'cleanup' | 'trace' | 'validate' | 'export', projectId: string, revisionId: string,
    settings?: { palette?: PaletteSettings; cleanup?: CleanupSettings; rerun_last_cleanup?: boolean; trace?: TraceSettings; colour_names?: Record<string, string> }) => fetch('/api/jobs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, project_id: projectId, revision_id: revisionId, ...settings }),
  }).then(result<Job>),
  cancelJob: (jobId: string) => fetch(`/api/jobs/${jobId}/cancel`, { method: 'POST' }).then(result<Job>),
  upscaleOptions: (signal?: AbortSignal) => fetch('/api/upscale/options', { signal }).then(result<UpscaleOptions>),
  upscale: (projectId: string, revisionId: string, settings: UpscaleSettings) => fetch(`/api/projects/${projectId}/revisions/${revisionId}/upscale`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ settings }),
  }).then(result<UpscaleStarted>),
}

export function followJob(jobId: string, onJob: (job: Job) => void, onError: (error: Event) => void) {
  const events = new EventSource(`/api/jobs/${jobId}/events`)
  events.addEventListener('job', event => {
    const job = JSON.parse((event as MessageEvent).data) as Job
    onJob(job)
    if (['succeeded', 'failed', 'cancelled'].includes(job.status)) events.close()
  })
  events.onerror = event => { if (events.readyState === EventSource.CLOSED) onError(event) }
  return () => events.close()
}
