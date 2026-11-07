export type Project = { id: string; slug: string; name: string; created_at: string; updated_at: string }
export type Revision = { id: string; project_id: string; parent_revision_id: string | null; kind: string; message: string | null; settings: Record<string, unknown>; created_at: string }
export type Artifact = { id: string; revision_id: string; kind: string; filename: string; media_type: string; byte_size: number; download_url: string }
export type Job = { id: string; project_id: string | null; revision_id: string | null; result_revision_id: string | null; kind: string; status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'; progress: number; message: string | null; error: string | null }
export type PaletteColour = { index: number; name: string; hex: string; rgb: [number, number, number]; share_percent: number }
export type EditorState = { width: number; height: number; palette: PaletteColour[]; requested_colour_count: number | null; protected_pixels: number; removed_colour_index?: number | null; inherited_remove_colour?: { value: string | null; unresolved: boolean; hex?: string | null; problem?: 'missing' | 'ambiguous' | null } | null }
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
// The trace's settings (vinylgen.contracts.TraceSettings): the soft node limits, which only
// warn, and each curve kind's tube half-width in px, which steers t3. Arcs, ellipses and
// cubics are null when off; the line is never off.
export type TraceSettings = {
  soft_path_nodes: number; soft_layer_nodes: number; soft_document_nodes: number
  half_line: number; half_arc: number | null; half_ellipse: number | null; half_cubic: number | null
}
// The curve kinds t3 draws, as its setting's `half` table names them.
export type CurveKind = 'line' | 'arc' | 'ellipse' | 'cubic'
// The tracer the server traces with: t3api, its build and its default setting in the
// canonical form (t3api.normal_setting): each kind's half-width (null off), A1, A2 and L.
export type TracerInfo = { name: string; build: string; setting: { half: Record<CurveKind, number | null>; A1: number; A2: number; L: number } }
// One face of a running trace as t3 emitted it (vinylgen.tracing.face_event): its planar-map
// face id, its colour's palette index, its rings as one path-data string (the `d` of its
// path in traced-edge.svg) and their sodipodi:nodetypes.
export type FaceEvent = { type: 'face'; face: number; label: number; rings: string; nodes: string }
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
export type Health = { status: 'ok'; max_upload_bytes: number; active_jobs?: number; tracer?: TracerInfo | null }
export type InkscapeHandoff = { exports: Artifact[]; shared_folder_enabled: boolean; guidance: string }
// Why the server refused a planar map saved from nib's editor (HTTP 422): `invalid` (t3 cannot
// read it, or its check finds `violations`), `mismatch` (another size or palette than the
// revision it edits) or `unchanged` (nothing was edited).
export type MapRefusal = { detail: string; reason: 'invalid' | 'mismatch' | 'unchanged'; problems: string[]; violations?: number }
export type MapSaved = { revision: Revision } | { refusal: MapRefusal }

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
  // A planar map edited in nib, the exact text nib wrote, saved for the trace revision that owns
  // the map it was opened from: a new trace revision, or the server's refusal.
  saveMap: (revisionId: string, text: string) => fetch(`/api/revisions/${revisionId}/map`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: text,
  }).then(async (response): Promise<MapSaved> => {
    if (response.status === 422) {
      const body = await response.clone().json().catch(() => null) as Partial<MapRefusal> | null
      if (body && typeof body.reason === 'string' && typeof body.detail === 'string') return { refusal: { problems: [], ...body } as MapRefusal }
    }
    return { revision: await result<Revision>(response) }
  }),
  cancelJob: (jobId: string) => fetch(`/api/jobs/${jobId}/cancel`, { method: 'POST' }).then(result<Job>),
  upscaleOptions: (signal?: AbortSignal) => fetch('/api/upscale/options', { signal }).then(result<UpscaleOptions>),
  upscale: (projectId: string, revisionId: string, settings: UpscaleSettings) => fetch(`/api/projects/${projectId}/revisions/${revisionId}/upscale`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ settings }),
  }).then(result<UpscaleStarted>),
}

// Follows a job's event stream: each state of the job, and during a trace each face as t3
// emits it (`onFace`, in t3's emission order).
export function followJob(jobId: string, onJob: (job: Job) => void, onError: (error: Event) => void, onFace?: (face: FaceEvent) => void) {
  const events = new EventSource(`/api/jobs/${jobId}/events`)
  if (onFace) events.addEventListener('face', event => onFace(JSON.parse((event as MessageEvent).data) as FaceEvent))
  events.addEventListener('job', event => {
    const job = JSON.parse((event as MessageEvent).data) as Job
    onJob(job)
    if (['succeeded', 'failed', 'cancelled'].includes(job.status)) events.close()
  })
  events.onerror = event => { if (events.readyState === EventSource.CLOSED) onError(event) }
  return () => events.close()
}
